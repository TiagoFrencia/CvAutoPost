"""
GetOnBoard applier — Playwright-based.

GetOnBoard jobs either:
  (a) Have a direct apply form on the platform
  (b) Redirect to an external company site

This applier handles case (a). For case (b) the job is skipped (too risky to follow
external redirects without knowing the target form structure).
"""
from pathlib import Path
from typing import Optional

import structlog
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from playwright_stealth import stealth_sync
from sqlalchemy.orm import Session

from ai_engine.cv_loader import get_pdf_path
from ai_engine.form_filler import FormFiller, OrphanQuestion
from core.models import Application, Job
from core.enums import ApplicationStatus
from core.playwright_config import CHROMIUM_ARGS, headless
from services.applier import ApplicationResult, BaseApplier, CaptchaDetected, register
from services.screenshot import capture

logger = structlog.get_logger()

GETONBOARD_DOMAIN = "getonboard.com"


@register
class GetOnBoardApplier(BaseApplier):
    platform_name = "getonboard"
    cv_profile_name = "remoto"

    def _do_apply(self, application: Application, job: Job) -> ApplicationResult:
        resume_step = self._get_resume_step(application)
        pdf_path = get_pdf_path(self.cv_profile_name)
        filler = FormFiller(self.cv_profile_name)
        orphans: list[dict] = []

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless(), args=CHROMIUM_ARGS)
            context = browser.new_context(
                viewport={"width": 1280, "height": 800},
                locale="es-AR",
            )
            page = context.new_page()
            stealth_sync(page)

            try:
                # Step 0: navigate to job URL
                if resume_step <= 0:
                    self._save_checkpoint(application, 0, "navigate")
                    page.goto(job.url, wait_until="domcontentloaded", timeout=30_000)
                    _check_captcha(page)

                # If the job URL redirects outside GetOnBoard → skip
                if GETONBOARD_DOMAIN not in page.url:
                    logger.info("getonboard.external_redirect", url=page.url, job_id=job.id)
                    browser.close()
                    return ApplicationResult(
                        False, ApplicationStatus.FAILED.value,
                        "External application — skipped (redirect to third-party site)",
                    )

                # Step 1: click apply button
                if resume_step <= 1:
                    self._save_checkpoint(application, 1, "click_apply")
                    try:
                        apply_btn = page.locator(
                            "a[class*='apply'], button[class*='apply'], "
                            "a:has-text('Apply'), a:has-text('Postular'), "
                            "button:has-text('Postular')"
                        ).first
                        apply_btn.click(timeout=8_000)
                        page.wait_for_load_state("domcontentloaded")
                        _check_captcha(page)
                    except PWTimeout:
                        screenshot = capture(page, f"no_apply_btn_{job.id}")
                        browser.close()
                        return ApplicationResult(
                            False, ApplicationStatus.FAILED.value,
                            "Apply button not found",
                            screenshot_path=screenshot,
                        )

                # Step 2: fill form fields
                if resume_step <= 2:
                    self._save_checkpoint(application, 2, "fill_form")
                    orphans = _fill_form(page, filler, pdf_path, job.id)

                # Step 3: submit
                if resume_step <= 3:
                    self._save_checkpoint(application, 3, "submit")
                    if orphans:
                        # Required unanswered questions — don't submit
                        browser.close()
                        return ApplicationResult(
                            False, ApplicationStatus.REVIEW_FORM.value,
                            orphan_questions=orphans,
                        )
                    _submit_form(page, job.id)

                browser.close()
                return ApplicationResult(True, ApplicationStatus.APPLIED.value)

            except CaptchaDetected:
                capture(page, f"captcha_{self.platform_name}_{job.id}")
                browser.close()
                raise
            except Exception as e:
                screenshot = capture(page, f"error_{self.platform_name}_{job.id}")
                browser.close()
                return ApplicationResult(
                    False, ApplicationStatus.FAILED.value,
                    str(e), screenshot_path=screenshot,
                )


def _check_captcha(page):
    captcha_signals = [
        "iframe[src*='captcha']",
        "iframe[src*='recaptcha']",
        "#cf-challenge-running",
        "[data-ray]",  # Cloudflare
    ]
    for selector in captcha_signals:
        if page.locator(selector).count() > 0:
            raise CaptchaDetected(f"CAPTCHA detected at {page.url}")


def _fill_form(page, filler: FormFiller, pdf_path: Path, job_id: int) -> list[dict]:
    """Fill all form inputs. Returns list of orphan question dicts for unanswerable required fields."""
    orphans = []

    # File upload (CV attachment)
    file_inputs = page.locator("input[type='file']")
    if file_inputs.count() > 0:
        file_inputs.first.set_input_files(str(pdf_path))
        logger.debug("getonboard.cv_attached", job_id=job_id)

    # Text / textarea fields
    inputs = page.locator("input[type='text']:visible, input[type='email']:visible, textarea:visible")
    for i in range(inputs.count()):
        inp = inputs.nth(i)
        label = _get_label(page, inp)
        if not label:
            continue
        is_required = inp.get_attribute("required") is not None

        try:
            answer = filler.fill(label, field_type="text", required=is_required)
            if answer:
                inp.fill(answer)
        except OrphanQuestion as oq:
            orphans.append({"field": oq.question_text, "required": is_required})

    return orphans


def _get_label(page, input_el) -> Optional[str]:
    """Try to find the label text associated with an input element."""
    try:
        input_id = input_el.get_attribute("id")
        if input_id:
            label = page.locator(f"label[for='{input_id}']")
            if label.count() > 0:
                return label.inner_text().strip()
        placeholder = input_el.get_attribute("placeholder")
        if placeholder:
            return placeholder
        name = input_el.get_attribute("name")
        return name
    except Exception:
        return None


def _submit_form(page, job_id: int):
    try:
        submit = page.locator(
            "button[type='submit']:visible, input[type='submit']:visible, "
            "button:has-text('Send'), button:has-text('Submit'), "
            "button:has-text('Enviar'), button:has-text('Postular')"
        ).first
        submit.click(timeout=8_000)
        page.wait_for_load_state("domcontentloaded", timeout=15_000)
        logger.info("getonboard.submitted", job_id=job_id)
    except PWTimeout as e:
        raise Exception(f"Submit button not found or page didn't load after submit: {e}")
