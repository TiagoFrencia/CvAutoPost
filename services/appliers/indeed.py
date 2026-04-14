"""
Indeed Argentina applier — Playwright + saved cookies.

Indeed's apply flow varies by job:
  (a) "Apply on Indeed" — form hosted on Indeed (single or multi-step wizard)
  (b) "Apply on company site" — external redirect (skipped)

For (a): inject cookies → navigate → click "Aplicar" → fill each form step
         (clicking "Continuar" between steps) → submit → verify success.
"""
from pathlib import Path
from typing import Optional

import structlog
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from playwright_stealth import stealth_sync

from ai_engine.cv_loader import get_pdf_path
from ai_engine.form_filler import FormFiller
from core.models import Application, Job
from core.enums import ApplicationStatus
from core.playwright_config import CHROMIUM_ARGS, headless
from services.applier import ApplicationResult, AuthExpired, BaseApplier, CaptchaDetected, register
from services.screenshot import capture

logger = structlog.get_logger()

BASE_URL = "https://ar.indeed.com"

APPLY_BTN_SELECTOR = "button#indeedApplyButton, button[class*='ia-IndeedApply'], span.iaLabel"
EXTERNAL_APPLY_SELECTOR = (
    "a[href*='clk'], "
    "span.indeed-apply-status--external, "
    "a:has-text('Solicitar en el sitio de la empresa'), "
    "button:has-text('Solicitar en el sitio de la empresa'), "
    "a:has-text('Apply on company site'), "
    "span:has-text('Solicitar en el sitio de la empresa')"
)
SUCCESS_SELECTOR = "[class*='ia-PostApply'], h2:has-text('Tu postulación fue enviada'), .ia-ApplyStepContent--done"
AUTH_SELECTOR = "#loginform, .auth-modal"
CAPTCHA_SELECTOR = (
    "iframe[src*='captcha'], #recaptcha, "
    "#challenge-running, #challenge-stage, "
    "iframe[src*='challenges.cloudflare'], "
    "iframe[title*='challenge']"
)
CAPTCHA_TEXT_MARKERS = [
    "Verificación adicional requerida",
    "Verifique que es un ser humano",
    "Verify you are human",
    "Additional verification required",
    "Security Check",
]
# "Continue" button between wizard steps
CONTINUE_BTN_SELECTOR = (
    "button:has-text('Continuar'):visible, "
    "button:has-text('Continue'):visible, "
    "button:has-text('Siguiente'):visible"
)
# Final submit button
SUBMIT_BTN_SELECTOR = (
    "button:has-text('Enviar'):visible, "
    "button:has-text('Submit'):visible, "
    "button:has-text('Enviar solicitud'):visible, "
    "button:has-text('Submit application'):visible, "
    "button[type='submit']:visible"
)


@register
class IndeedApplier(BaseApplier):
    platform_name = "indeed"
    cv_profile_name = "local"

    def _do_apply(self, application: Application, job: Job) -> ApplicationResult:
        if not self.session_manager.has_cookies("indeed"):
            raise AuthExpired("No cookies for indeed")

        resume_step = self._get_resume_step(application)
        cv_profile_name = self._get_application_cv_profile_name(application)
        pdf_path = get_pdf_path(cv_profile_name)
        filler = FormFiller(cv_profile_name)

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless(), args=CHROMIUM_ARGS)
            context = browser.new_context(
                locale="es-AR",
                viewport={"width": 1280, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
            )
            self.session_manager.inject_into_playwright(context, "indeed")
            page = context.new_page()
            stealth_sync(page)

            try:
                # Step 0: navigate — convert rc/clk tracking URLs to canonical viewjob URLs
                if resume_step <= 0:
                    self._save_checkpoint(application, 0, "navigate")
                    nav_url = job.url
                    if "rc/clk" in nav_url or "clk?" in nav_url:
                        from urllib.parse import urlparse, parse_qs
                        parsed = urlparse(nav_url)
                        jk = parse_qs(parsed.query).get("jk", [None])[0]
                        if jk:
                            nav_url = f"{BASE_URL}/viewjob?jk={jk}"
                    page.goto(nav_url, wait_until="domcontentloaded", timeout=30_000)
                    _check_auth(page)
                    _check_captcha(page)

                # Wait a moment for React to render the apply buttons
                page.wait_for_timeout(2_000)

                # Check for external application — mark as SKIPPED (not a failure)
                if page.locator(EXTERNAL_APPLY_SELECTOR).count() > 0:
                    browser.close()
                    return ApplicationResult(
                        False, ApplicationStatus.SKIPPED.value,
                        "External application on company site — skipped",
                    )

                # Step 1: click apply (only for "Apply on Indeed" jobs)
                if resume_step <= 1:
                    self._save_checkpoint(application, 1, "click_apply")
                    apply_btn = page.locator(APPLY_BTN_SELECTOR)
                    if apply_btn.count() == 0:
                        browser.close()
                        return ApplicationResult(
                            False, ApplicationStatus.SKIPPED.value,
                            "No Indeed-hosted apply button found — likely external",
                        )
                    try:
                        apply_btn.first.click(timeout=8_000)
                        page.wait_for_load_state("domcontentloaded", timeout=15_000)
                        _check_auth(page)
                        _check_captcha(page)
                    except PWTimeout:
                        screenshot = capture(page, f"no_apply_btn_{job.id}")
                        browser.close()
                        return ApplicationResult(
                            False, ApplicationStatus.FAILED.value,
                            "Apply button click timed out",
                            screenshot_path=screenshot,
                        )

                # Step 2: fill multi-step form
                if resume_step <= 2:
                    self._save_checkpoint(application, 2, "fill_form")
                    _check_captcha(page)
                    _fill_wizard(page, filler, pdf_path, job.id)

                # Step 3: submit (final page)
                if resume_step <= 3:
                    self._save_checkpoint(application, 3, "submit")
                    _submit(page, job.id)

                # Step 4: verify
                if resume_step <= 4:
                    self._save_checkpoint(application, 4, "verify")
                    try:
                        page.wait_for_selector(SUCCESS_SELECTOR, timeout=10_000)
                    except PWTimeout:
                        screenshot = capture(page, f"unconfirmed_{job.id}")
                        browser.close()
                        return ApplicationResult(
                            False, ApplicationStatus.FAILED.value,
                            "Could not confirm submission",
                            screenshot_path=screenshot,
                        )

                browser.close()
                return ApplicationResult(True, ApplicationStatus.APPLIED.value)

            except CaptchaDetected as e:
                screenshot = capture(page, f"auth_captcha_indeed_{job.id}")
                browser.close()
                # smartapply.indeed.com always blocks Playwright via Cloudflare WAF —
                # treat as SKIPPED (not a transient failure that should trip circuit breaker)
                if "smartapply.indeed.com" in str(e):
                    return ApplicationResult(
                        False, ApplicationStatus.SKIPPED.value,
                        f"Indeed apply blocked by Cloudflare WAF (smartapply): {e}",
                        screenshot_path=screenshot,
                    )
                raise
            except AuthExpired:
                capture(page, f"auth_captcha_indeed_{job.id}")
                browser.close()
                raise
            except Exception as e:
                screenshot = capture(page, f"error_indeed_{job.id}")
                browser.close()
                return ApplicationResult(
                    False, ApplicationStatus.FAILED.value,
                    str(e), screenshot_path=screenshot,
                )


def _check_auth(page):
    if page.locator(AUTH_SELECTOR).count() > 0:
        raise AuthExpired("Indeed session expired")


def _check_captcha(page):
    if page.locator(CAPTCHA_SELECTOR).count() > 0:
        raise CaptchaDetected(f"CAPTCHA at {page.url}")
    try:
        url = page.url
        if "__cf_chl_rt_tk" in url or "__cf_chl_f_tk" in url:
            raise CaptchaDetected(f"Cloudflare challenge at {url}")
        title = page.title()
        if "security check" in title.lower() or "just a moment" in title.lower():
            raise CaptchaDetected(f"Cloudflare challenge at {url}")
        for marker in CAPTCHA_TEXT_MARKERS:
            if page.locator(f"text={marker}").count() > 0:
                raise CaptchaDetected(f"Cloudflare challenge at {url}")
    except CaptchaDetected:
        raise
    except Exception:
        pass


def _fill_wizard(page, filler: FormFiller, pdf_path: Path, job_id: int) -> None:
    """
    Fill Indeed's multi-step application wizard.
    Keeps clicking "Continuar" between steps until we reach the final submit page.
    Max 10 steps as a safety guard.
    """
    for step in range(10):
        _fill_current_step(page, filler, pdf_path, job_id)

        # If success already shown, stop early
        if page.locator(SUCCESS_SELECTOR).count() > 0:
            logger.info("indeed.early_success", job_id=job_id, step=step)
            return

        # Click "Continuar" if present (intermediate step)
        continue_btn = page.locator(CONTINUE_BTN_SELECTOR)
        if continue_btn.count() > 0:
            try:
                continue_btn.first.click(timeout=5_000)
                page.wait_for_load_state("domcontentloaded", timeout=15_000)
                page.wait_for_timeout(500)
                logger.debug("indeed.wizard_step", job_id=job_id, step=step)
                continue
            except PWTimeout:
                logger.debug("indeed.continue_timeout", job_id=job_id, step=step)

        # No "Continuar" means we're on the final page (submit or already done)
        break


def _fill_current_step(page, filler: FormFiller, pdf_path: Path, job_id: int) -> None:
    """Fill all visible fields on the current wizard step."""
    # CV/resume file upload
    file_inputs = page.locator("input[type='file']")
    if file_inputs.count() > 0:
        try:
            file_inputs.first.set_input_files(str(pdf_path))
        except Exception as e:
            logger.debug("indeed.file_upload_error", job_id=job_id, error=str(e))

    # Text / number / email / tel / textarea
    inputs = page.locator(
        "input[type='text']:visible, input[type='tel']:visible, "
        "input[type='number']:visible, textarea:visible"
    )
    for i in range(inputs.count()):
        inp = inputs.nth(i)
        try:
            if inp.input_value().strip():
                continue
        except Exception:
            continue
        label = _get_label(page, inp)
        if not label:
            continue
        answer = filler.fill(label, field_type=inp.get_attribute("type") or "text")
        if answer:
            inp.fill(answer)

    # Select / dropdown
    selects = page.locator("select:visible")
    for i in range(selects.count()):
        sel = selects.nth(i)
        try:
            current = sel.input_value()
            if current and current not in ("", "0", "-1"):
                continue
        except Exception:
            continue
        label = _get_label(page, sel)
        if not label:
            continue
        options = page.evaluate("""
            (el) => Array.from(el.options)
                .filter(o => o.value && o.value !== '' && o.value !== '0' && o.value !== '-1')
                .map(o => ({value: o.value, text: o.text.trim()}))
        """, sel.element_handle())
        if not options:
            continue
        options_text = ", ".join(f'"{o["text"]}"' for o in options)
        answer = filler.fill(f'{label} (opciones: {options_text})', field_type="dropdown")
        if answer:
            answer_norm = answer.lower().strip()
            matched = next(
                (o["value"] for o in options
                 if answer_norm in o["text"].lower() or o["text"].lower() in answer_norm),
                None,
            )
            if matched:
                try:
                    sel.select_option(matched)
                    logger.debug("indeed.select_filled", label=label, value=matched)
                except Exception:
                    pass

    # Radio button groups
    _fill_radio_groups(page, filler)

    # Checkboxes
    _fill_checkboxes(page, filler)


def _fill_radio_groups(page, filler: FormFiller) -> None:
    """Handle radio button groups (Sí/No, experience levels, etc.)."""
    try:
        radio_names = page.evaluate("""
            () => {
                const inputs = document.querySelectorAll('input[type="radio"]:not([disabled])');
                const names = new Set();
                inputs.forEach(i => { if (i.name) names.add(i.name); });
                return [...names];
            }
        """)
    except Exception:
        return

    for name in radio_names:
        already_checked = page.evaluate(
            f"""() => !!document.querySelector('input[type="radio"][name="{name}"]:checked')"""
        )
        if already_checked:
            continue

        question = page.evaluate(f"""
            () => {{
                const radio = document.querySelector('input[type="radio"][name="{name}"]');
                if (!radio) return null;
                const fieldset = radio.closest('fieldset');
                if (fieldset) {{
                    const legend = fieldset.querySelector('legend');
                    if (legend) return legend.textContent.trim();
                }}
                let el = radio.parentElement;
                for (let i = 0; i < 6; i++) {{
                    if (!el || el.tagName === 'FORM' || el.tagName === 'BODY') break;
                    const candidates = el.querySelectorAll('p, legend, label:not([for]), span[class*="label"], div[class*="label"]');
                    for (const c of candidates) {{
                        const t = c.textContent.trim();
                        if (t.length > 5 && t.length < 300 && !t.includes('\\n')) return t;
                    }}
                    el = el.parentElement;
                }}
                return name;
            }}
        """) or name

        options = page.evaluate(f"""
            () => {{
                const radios = Array.from(document.querySelectorAll('input[type="radio"][name="{name}"]'));
                return radios.map(r => {{
                    const lbl = document.querySelector(`label[for="${{r.id}}"]`);
                    return {{ value: r.value, label: lbl ? lbl.textContent.trim() : r.value }};
                }});
            }}
        """)

        if not options:
            continue

        try:
            answer = filler.fill(question, field_type="boolean")
            if not answer:
                continue
            answer_norm = answer.lower().strip()
            clicked = False
            for opt in options:
                opt_label = opt["label"].lower().strip()
                opt_value = opt["value"].lower().strip()
                if answer_norm in ("sí", "si", "yes", "true", "1") and opt_label in ("sí", "si", "yes"):
                    _click_radio(page, name, opt["value"])
                    clicked = True
                    break
                elif answer_norm in ("no", "false", "0") and opt_label == "no":
                    _click_radio(page, name, opt["value"])
                    clicked = True
                    break
                elif opt_label == answer_norm or opt_value == answer_norm:
                    _click_radio(page, name, opt["value"])
                    clicked = True
                    break
            if not clicked and options:
                logger.debug("indeed.radio_fallback_first", question=question, answer=answer)
                _click_radio(page, name, options[0]["value"])
        except Exception as e:
            logger.debug("indeed.radio_fill_error", question=question, error=str(e))


def _fill_checkboxes(page, filler: FormFiller) -> None:
    """Auto-check terms/conditions; ask LLM for other checkboxes."""
    try:
        checkboxes = page.locator("input[type='checkbox']:visible:not(:checked)")
        for i in range(checkboxes.count()):
            cb = checkboxes.nth(i)
            try:
                label = _get_closest_label(page, cb) or cb.get_attribute("name") or ""
                label_lower = label.lower()
                auto_check_keywords = ("término", "termino", "condicion", "condición",
                                       "privacidad", "acepto", "acepta", "política",
                                       "politica", "terms", "privacy", "agree")
                if any(kw in label_lower for kw in auto_check_keywords):
                    cb.check(timeout=3_000)
                    continue
                if label:
                    answer = filler.fill(label, field_type="boolean")
                    if answer and answer.lower().strip() in ("sí", "si", "yes", "true", "1"):
                        cb.check(timeout=3_000)
            except Exception as e:
                logger.debug("indeed.checkbox_fill_error", error=str(e))
    except Exception as e:
        logger.debug("indeed.fill_checkboxes_error", error=str(e))


def _click_radio(page, name: str, value: str):
    try:
        page.locator(f"input[type='radio'][name='{name}'][value='{value}']").first.click(timeout=3_000)
    except Exception:
        page.evaluate(f"""
            () => {{
                const r = document.querySelector('input[type="radio"][name="{name}"][value="{value}"]');
                if (r) r.click();
            }}
        """)


def _get_label(page, input_el) -> Optional[str]:
    try:
        input_id = input_el.get_attribute("id")
        if input_id:
            lbl = page.locator(f"label[for='{input_id}']")
            if lbl.count() > 0:
                return lbl.inner_text().strip()
        return input_el.get_attribute("placeholder") or input_el.get_attribute("name")
    except Exception:
        return None


def _get_closest_label(page, input_el) -> Optional[str]:
    try:
        return page.evaluate("""
            (el) => {
                if (el.id) {
                    const lbl = document.querySelector(`label[for="${el.id}"]`);
                    if (lbl) return lbl.textContent.trim();
                }
                if (el.getAttribute('placeholder')) return el.getAttribute('placeholder');
                let parent = el.parentElement;
                for (let i = 0; i < 5; i++) {
                    if (!parent || parent.tagName === 'FORM' || parent.tagName === 'BODY') break;
                    for (const sib of parent.children) {
                        if (sib === el || sib.contains(el)) break;
                        const t = sib.textContent.trim();
                        if (t.length > 3 && t.length < 300) return t;
                    }
                    parent = parent.parentElement;
                }
                return el.getAttribute('name') || null;
            }
        """, input_el.element_handle())
    except Exception:
        return None


def _submit(page, job_id: int):
    """Click the final submit button. Indeed uses 'Enviar solicitud' on the review page."""
    # Check if already on success page
    if page.locator(SUCCESS_SELECTOR).count() > 0:
        logger.info("indeed.already_submitted", job_id=job_id)
        return

    try:
        btn = page.locator(SUBMIT_BTN_SELECTOR).first
        btn.click(timeout=8_000)
        page.wait_for_load_state("domcontentloaded", timeout=20_000)
        logger.info("indeed.submitted", job_id=job_id)
    except PWTimeout as e:
        raise Exception(f"Submit failed: {e}")
