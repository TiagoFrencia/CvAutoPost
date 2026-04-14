"""
LinkedIn applier — Nodriver (CDP to real Chrome) + Easy Apply.

⚠️  LinkedIn uses Cloudflare + fingerprint detection (JA3/JA4 + WebRTC leaks).
    Playwright's Chromium is detected. We connect to the user's real Chrome via CDP.
    This requires Chrome to be running with --remote-debugging-port=9222
    OR we launch it ourselves via Nodriver (which handles the CDP connection).

Easy Apply flow:
  0. Navigate to job URL with cookies
  1. Detect "Easy Apply" button (not "Apply on company website")
  2. Click → modal opens
  3. Fill each form step (may be multi-step)
  4. Submit final step
  5. Verify confirmation

Hard daily limit: MAX_APPLICATIONS enforced in run_apply_queue via platform.daily_limit=5.
Circuit breaker: any CAPTCHA or unusual redirect → pause LinkedIn 24h.
"""
import asyncio
from datetime import datetime
from pathlib import Path
from typing import Optional

import structlog

from ai_engine.cv_loader import get_pdf_path
from ai_engine.form_filler import FormFiller
from core.config import settings
from core.models import Application, Job
from core.enums import ApplicationStatus
from services.applier import ApplicationResult, AuthExpired, BaseApplier, CaptchaDetected, register
from services.screenshot import capture

logger = structlog.get_logger()

EASY_APPLY_BTN_SELECTOR = (
    "button.jobs-apply-button[aria-label*='Easy Apply'], "
    "button.jobs-apply-button[aria-label*='Solicitud sencilla'], "
    "button.jobs-apply-button:has-text('Easy Apply'), "
    "button.jobs-apply-button:has-text('Solicitud sencilla')"
)
NEXT_BTN_SELECTOR = (
    "button[aria-label='Continue to next step'], "
    "button[aria-label='Review your application'], "
    "button[aria-label='Continuar con el siguiente paso'], "
    "button[aria-label='Revisar tu solicitud'], "
    "button[aria-label='Revisar solicitud']"
)
SUBMIT_BTN_SELECTOR = (
    "button[aria-label='Submit application'], "
    "button[aria-label='Enviar solicitud'], "
    "button[aria-label='Enviar candidatura']"
)
SUCCESS_SELECTOR = (
    "div.jobs-easy-apply-modal--completed, "
    "h3:has-text('Your application was sent'), "
    "h3:has-text('Se ha enviado tu solicitud'), "
    "h3:has-text('Tu solicitud se ha enviado')"
)
CAPTCHA_SELECTOR = "iframe[src*='checkpoint'], #challenge-running, .artdeco-modal--captcha"
AUTH_SELECTOR = ".login__form, [data-test-id='auth-modal']"


@register
class LinkedInApplier(BaseApplier):
    platform_name = "linkedin"
    cv_profile_name = "remoto"

    def _do_apply(self, application: Application, job: Job) -> ApplicationResult:
        if not self.session_manager.has_cookies("linkedin"):
            raise AuthExpired("No cookies for linkedin. Run: python login_helper.py --platform linkedin")

        cookies = self.session_manager.load_cookies("linkedin")
        cv_profile_name = self._get_application_cv_profile_name(application)
        pdf_path = get_pdf_path(cv_profile_name)
        filler = FormFiller(cv_profile_name)

        try:
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", ResourceWarning)
                return asyncio.run(
                    self._async_apply(application, job, cookies, pdf_path, filler)
                )
        except CaptchaDetected:
            raise
        except AuthExpired:
            raise
        except Exception as e:
            logger.error("linkedin.apply_error", job_id=job.id, error=str(e))
            return ApplicationResult(False, ApplicationStatus.FAILED.value, str(e))

    async def _async_apply(
        self,
        application: Application,
        job: Job,
        cookies: list[dict],
        pdf_path: Path,
        filler: FormFiller,
    ) -> ApplicationResult:
        import nodriver as uc
        from pathlib import Path as _Path
        import sys as _sys

        chrome_path = settings.chrome_executable_path
        if chrome_path and not _Path(chrome_path).exists():
            # Chrome not found at configured path (e.g. running inside Docker on Linux
            # while Chrome is installed on the Windows host). LinkedIn apply cannot proceed.
            raise AuthExpired(
                f"Chrome not found at '{chrome_path}'. "
                "LinkedIn applier requires real Chrome on the host machine. "
                "Run the bot directly with 'python main.py' on Windows, not inside Docker."
            )

        # On Linux (Docker), start a virtual display so Chrome can run without a real screen.
        xvfb_proc = None
        import os as _os
        if _sys.platform == "linux":
            import subprocess as _sp
            _os.environ.setdefault("DISPLAY", ":99")
            xvfb_proc = _sp.Popen(
                ["Xvfb", ":99", "-screen", "0", "1280x800x24"],
                stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
            )
            await asyncio.sleep(1)  # let Xvfb initialize

        browser = await uc.start(
            browser_executable_path=chrome_path,
            headless=False,  # nodriver works best with visible Chrome (Xvfb provides the display on Linux)
            sandbox=_sys.platform != "linux",  # Docker needs --no-sandbox
        )
        tab = None
        try:
            logger.info("linkedin.navigating", url=job.url)
            tab = await browser.get(job.url)
            logger.info("linkedin.navigated")
            await _inject_cookies_nodriver(tab, cookies)
            logger.info("linkedin.cookies_injected")
            await tab.reload()
            logger.info("linkedin.reloaded")
            await tab.sleep(2)
            logger.info("linkedin.checking_auth")

            # Check auth
            if await _has_element(tab, AUTH_SELECTOR):
                raise AuthExpired("LinkedIn session expired. Re-run login_helper.py")
            logger.info("linkedin.auth_ok")

            # Check captcha
            if await _has_element(tab, CAPTCHA_SELECTOR):
                raise CaptchaDetected("LinkedIn CAPTCHA detected")
            logger.info("linkedin.captcha_ok")

            # Step 0: verify Easy Apply button exists
            resume_step = self._get_resume_step(application)
            if resume_step <= 0:
                self._save_checkpoint(application, 0, "check_easy_apply")
                has_easy_apply = await _has_element(tab, EASY_APPLY_BTN_SELECTOR, timeout=8)
                if not has_easy_apply:
                    logger.info("linkedin.no_easy_apply", job_id=job.id)
                    browser.stop()
                    return ApplicationResult(
                        False, ApplicationStatus.FAILED.value,
                        "No Easy Apply button — requires external application",
                    )

            # Step 1: click Easy Apply
            if resume_step <= 1:
                self._save_checkpoint(application, 1, "click_easy_apply")
                btn = await tab.find(EASY_APPLY_BTN_SELECTOR, timeout=8)
                if not btn:
                    browser.stop()
                    return ApplicationResult(
                        False, ApplicationStatus.FAILED.value,
                        "Easy Apply button disappeared before click",
                    )
                await btn.click()
                await tab.sleep(1.5)

            # Step 2: fill form (may be multi-step)
            if resume_step <= 2:
                self._save_checkpoint(application, 2, "fill_form")
                await _fill_easy_apply_form(tab, filler, pdf_path, job.id, self)

            # Step 3: submit
            if resume_step <= 3:
                self._save_checkpoint(application, 3, "submit")
                has_submit = await _has_element(tab, SUBMIT_BTN_SELECTOR, timeout=8)
                if not has_submit:
                    browser.stop()
                    return ApplicationResult(
                        False, ApplicationStatus.FAILED.value,
                        "Submit button not found in Easy Apply modal",
                    )
                submit_btn = await tab.find(SUBMIT_BTN_SELECTOR, timeout=5)
                await submit_btn.click()
                await tab.sleep(2)

            # Step 4: verify
            if resume_step <= 4:
                self._save_checkpoint(application, 4, "verify")
                success = await _has_element(tab, SUCCESS_SELECTOR, timeout=8)
                if not success:
                    browser.stop()
                    return ApplicationResult(
                        False, ApplicationStatus.FAILED.value,
                        "Could not confirm LinkedIn Easy Apply submission",
                    )

            # Refresh and persist cookies so the LinkedIn session stays alive longer
            try:
                await self.session_manager.save_refreshed_cookies_nodriver(tab, "linkedin")
                logger.info("linkedin.cookies_refreshed")
            except Exception as cookie_err:
                logger.warning("linkedin.cookie_refresh_failed", error=str(cookie_err))

            browser.stop()
            if xvfb_proc:
                xvfb_proc.terminate()
            return ApplicationResult(True, ApplicationStatus.APPLIED.value)

        except (CaptchaDetected, AuthExpired):
            if browser:
                browser.stop()
            if xvfb_proc:
                xvfb_proc.terminate()
            raise
        except Exception as e:
            if browser:
                browser.stop()
            if xvfb_proc:
                xvfb_proc.terminate()
            raise


async def _inject_cookies_nodriver(tab, cookies: list[dict]):
    """Inject saved cookies into nodriver tab via CDP Network.setCookies."""
    import nodriver.cdp.network as cdp_network
    try:
        params = []
        for c in cookies:
            params.append(cdp_network.CookieParam(
                name=c.get("name", ""),
                value=c.get("value", ""),
                domain=c.get("domain"),
                path=c.get("path", "/"),
                secure=c.get("secure", False),
                http_only=c.get("httpOnly", False),
            ))
        if params:
            await tab.send(cdp_network.set_cookies(params))
    except Exception as e:
        logger.warning("linkedin.cookie_inject_failed", error=str(e))


async def _has_element(tab, selector: str, timeout: int = 5) -> bool:
    try:
        el = await tab.find(selector, timeout=timeout)
        return el is not None
    except Exception:
        return False


async def _fill_easy_apply_form(tab, filler: FormFiller, pdf_path: Path, job_id: int, applier) -> None:
    """Fill the Easy Apply multi-step form. Clicks 'Next' until Submit is visible."""
    step = 0
    max_steps = 10  # safety cap

    while step < max_steps:
        step += 1

        # Attach resume if file upload present
        try:
            file_input = await tab.find("input[type='file']", timeout=2)
            if file_input:
                await file_input.send_file(str(pdf_path))
                logger.debug("linkedin.cv_attached", job_id=job_id)
        except Exception:
            pass

        # Fill visible text fields
        await _fill_visible_fields(tab, filler, job_id)

        # Check if Submit button is now available
        submit = await tab.find(SUBMIT_BTN_SELECTOR, timeout=2)
        if submit:
            break

        # Click Next / Continue
        next_btn = await tab.find(NEXT_BTN_SELECTOR, timeout=3)
        if not next_btn:
            break
        await next_btn.click()
        await tab.sleep(1)

        # Check for captcha after each step
        if await _has_element(tab, CAPTCHA_SELECTOR, timeout=2):
            raise CaptchaDetected("LinkedIn CAPTCHA appeared during Easy Apply form")


async def _fill_visible_fields(tab, filler: FormFiller, job_id: int) -> None:
    """Fill all visible text/select inputs in the current Easy Apply step."""
    try:
        inputs = await tab.find_all(
            "input[type='text']:not([type='hidden']), "
            "input[type='number'], "
            "textarea",
            timeout=3,
        )
        for inp in (inputs or []):
            try:
                label = await _get_nodriver_label(tab, inp)
                if not label:
                    continue
                # Skip if already has a value
                current = await inp.get_attribute("value") or ""
                if current.strip():
                    continue
                answer = filler.fill(label, field_type="text")
                if answer:
                    await inp.clear_input()
                    await inp.send_keys(answer)
            except Exception:
                pass
    except Exception as e:
        logger.debug("linkedin.fill_fields_error", error=str(e))


async def _get_nodriver_label(tab, input_el) -> Optional[str]:
    try:
        input_id = await input_el.get_attribute("id")
        if input_id:
            lbl = await tab.find(f"label[for='{input_id}']", timeout=1)
            if lbl:
                return (await lbl.get_html()).strip()
        placeholder = await input_el.get_attribute("placeholder")
        if placeholder:
            return placeholder
        aria = await input_el.get_attribute("aria-label")
        return aria
    except Exception:
        return None
