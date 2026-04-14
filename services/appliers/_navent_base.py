"""
Shared Playwright applier logic for Navent-network platforms (ZonaJobs, Bumeran).
Both sites now use a mix of classic "Postularme" and "Postulacion rapida" flows.
"""
from pathlib import Path
from typing import Optional

import structlog
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright
from playwright_stealth import stealth_sync

from ai_engine.cv_loader import get_pdf_path
from ai_engine.form_filler import FormFiller
from core.enums import ApplicationStatus
from core.models import Application, Job
from core.playwright_config import CHROMIUM_ARGS, headless
from services.applier import (
    ApplicationResult,
    AuthExpired,
    BaseApplier,
    CaptchaDetected,
)
from services.screenshot import capture

logger = structlog.get_logger()

APPLY_BTN_SELECTOR = (
    "button[form='form-salario-pretendido'], "
    "button.applyBtn, "
    "a.applyBtn, "
    "button[data-qa='btn-apply'], "
    "button:has-text('Postulación rápida'), "
    "button:has-text('Postularme'), "
    "a:has-text('Postularme')"
)
SUCCESS_SELECTOR = (
    ".postulation-success, "
    "[class*='postulation-success'], "
    "h1:has-text('postulación enviada'), "
    "h2:has-text('postulación enviada'), "
    "text=Ver estadísticas, "
    "text=Actualizar mi CV"
)
AUTH_SELECTOR = "a[href*='/login'], .login-modal, .auth-required"
CAPTCHA_SELECTOR = "iframe[src*='captcha'], #challenge-running"


class NaventApplier(BaseApplier):
    """Abstract base for ZonaJobs/Bumeran. Subclasses set platform_name + cv_profile_name."""

    def _do_apply(self, application: Application, job: Job) -> ApplicationResult:
        if not self.session_manager.has_cookies(self.platform_name):
            raise AuthExpired(f"No cookies for {self.platform_name}")

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
            self.session_manager.inject_into_playwright(context, self.platform_name)
            page = context.new_page()
            stealth_sync(page)

            try:
                if resume_step <= 0:
                    self._save_checkpoint(application, 0, "navigate")
                    page.goto(job.url, wait_until="domcontentloaded", timeout=30_000)
                    _check_auth(page)
                    _check_captcha(page)

                if resume_step <= 1:
                    self._save_checkpoint(application, 1, "click_apply")
                    if _verify_success(page, job.url):
                        browser.close()
                        return ApplicationResult(True, ApplicationStatus.APPLIED.value)
                    # Fill salary field if present (ZonaJobs postulación rápida form)
                    _fill_salary_if_present(page)
                    try:
                        page.locator(APPLY_BTN_SELECTOR).first.click(timeout=8_000, force=True)
                        page.wait_for_load_state("domcontentloaded", timeout=15_000)
                        _check_auth(page)
                        _check_captcha(page)
                    except PWTimeout:
                        screenshot = capture(page, f"no_apply_btn_{job.id}")
                        browser.close()
                        return ApplicationResult(
                            False,
                            ApplicationStatus.FAILED.value,
                            "Apply button not found",
                            screenshot_path=screenshot,
                        )

                if resume_step <= 2:
                    self._save_checkpoint(application, 2, "fill_form")
                    _fill_form(page, filler, pdf_path)

                if resume_step <= 3:
                    self._save_checkpoint(application, 3, "submit")
                    _submit(page, job.id, job.url)

                if resume_step <= 4:
                    self._save_checkpoint(application, 4, "verify")
                    if not _verify_success(page, job.url):
                        screenshot = capture(page, f"unconfirmed_{job.id}")
                        browser.close()
                        return ApplicationResult(
                            False,
                            ApplicationStatus.FAILED.value,
                            "Could not confirm submission",
                            screenshot_path=screenshot,
                        )

                browser.close()
                return ApplicationResult(True, ApplicationStatus.APPLIED.value)

            except (CaptchaDetected, AuthExpired):
                capture(page, f"auth_{self.platform_name}_{job.id}")
                browser.close()
                raise
            except Exception as e:
                screenshot = capture(page, f"error_{self.platform_name}_{job.id}")
                browser.close()
                return ApplicationResult(
                    False,
                    ApplicationStatus.FAILED.value,
                    str(e),
                    screenshot_path=screenshot,
                )


def _check_auth(page):
    if page.locator(AUTH_SELECTOR).count() > 0:
        raise AuthExpired(f"Session expired at {page.url}")


def _check_captcha(page):
    if page.locator(CAPTCHA_SELECTOR).count() > 0:
        raise CaptchaDetected(f"CAPTCHA at {page.url}")


def _fill_form(page, filler: FormFiller, pdf_path: Path) -> None:
    # CV PDF upload
    file_inputs = page.locator("input[type='file']")
    if file_inputs.count() > 0:
        try:
            file_inputs.first.set_input_files(str(pdf_path))
        except Exception as e:
            logger.debug("navent.file_upload_error", error=str(e))

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
                    logger.debug("navent.select_filled", label=label, value=matched)
                except Exception:
                    pass

    # Radio button groups
    _fill_radio_groups(page, filler)

    # Checkboxes
    _fill_checkboxes(page, filler)


def _fill_radio_groups(page, filler: FormFiller) -> None:
    """Handle radio button groups."""
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
                    const candidates = el.querySelectorAll('p, legend, label:not([for]), span[class*="label"]');
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
                logger.debug("navent.radio_fallback_first", question=question, answer=answer)
                _click_radio(page, name, options[0]["value"])
        except Exception as e:
            logger.debug("navent.radio_fill_error", question=question, error=str(e))


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
                logger.debug("navent.checkbox_fill_error", error=str(e))
    except Exception as e:
        logger.debug("navent.fill_checkboxes_error", error=str(e))


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


def _submit(page, job_id: int, original_url: str):
    if _verify_success(page, original_url):
        logger.info("navent.already_applied_or_redirected", job_id=job_id)
        return

    try:
        btn = page.locator(
            "button[type='submit']:visible, "
            "button:has-text('Enviar'):visible, "
            "button:has-text('Postularme'):visible"
        ).first
        btn.click(timeout=8_000)
        page.wait_for_load_state("domcontentloaded", timeout=20_000)
        logger.info("navent.submitted", job_id=job_id)
    except PWTimeout as e:
        if _verify_success(page, original_url):
            logger.info("navent.submitted_after_timeout", job_id=job_id)
            return
        raise Exception(f"Submit failed: {e}") from e


def _fill_salary_if_present(page) -> None:
    """Fill ZonaJobs #salarioPretendido if visible and empty. Field only accepts digits."""
    try:
        salary_input = page.locator("#salarioPretendido")
        if salary_input.count() == 0:
            return
        if salary_input.input_value(timeout=2_000):
            return  # already filled
        salary_input.fill("1300000", timeout=3_000)
        logger.debug("navent.salary_filled")
    except Exception:
        pass  # non-critical — form still submits without it


def _verify_success(page, original_url: str) -> bool:
    for selector in SUCCESS_SELECTOR.split(", "):
        try:
            if page.locator(selector).count() > 0:
                return True
        except Exception:
            continue

    try:
        current_url = page.url
    except Exception:
        current_url = ""

    if "postulacion-rapida" in current_url and current_url != original_url:
        return True

    return False
