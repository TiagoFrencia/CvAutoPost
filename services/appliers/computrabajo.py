"""
Computrabajo applier - Playwright + saved cookies + playwright-stealth.

Flow:
  0. inject cookies -> navigate to job URL
  1. dismiss blocking overlays and click "Postularme"
  2. detect form type (simple confirm vs. full form)
  3. fill fields (FormFiller) + attach CV PDF
  4. submit
  5. verify success confirmation
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
    register,
)
from services.screenshot import capture

logger = structlog.get_logger()

# Selectors kept broad because Computrabajo uses multiple CTA variants.
APPLY_BTN_SELECTOR = (
    "a[data-href-offer-apply], "   # Computrabajo: <a data-href-offer-apply="..."> Postularme </a>
    "a[data-apply-link], "
    "button#applyBtn, "
    "a.applyBtn, "
    "button[data-id='applyBtn'], "
    "button:has-text('Postularme'), "
    "a:has-text('Postularme'), "
    "span:has-text('Postularme')"
)
CONFIRM_SELECTOR = (
    "button:has-text('Confirmar'), a:has-text('Confirmar'), "
    "button:has-text('Enviar postulación'), a:has-text('Enviar postulación'), "
    "button:has-text('Enviar mi CV'), a:has-text('Enviar mi CV'), "
    "button:has-text('Enviar CV'), a:has-text('Enviar CV'), "
    "input[type='submit'][value*='Enviar']:visible, "
    "input[type='submit'][value*='Confirmar']:visible, "
    "button[type='submit']:visible, "
    "input[type='submit']:visible"
)
SUCCESS_SELECTOR = (
    ".postulation-success, "
    "[class*='success'], "
    "h1:has-text('postulación enviada'), "
    "text=Postulado, "
    "text=Ya aplicaste a esta oferta, "
    "text=Te postulaste correctamente, "
    "text=postulaste correctamente, "
    "text=Ya te postulaste a esta oferta, "
    "text=te postulaste a esta oferta, "
    "text=Tu postulación fue enviada, "
    "text=Postulación enviada, "
    "h2:has-text('Postulación enviada'), "
    "h3:has-text('Postulación enviada')"
)
# Selectors that indicate the user has already applied (button state change after instant-apply)
ALREADY_APPLIED_SELECTOR = (
    "button:has-text('Ya aplicaste'), "
    "a:has-text('Ya aplicaste'), "
    "button:has-text('Postulado'), "
    "a:has-text('Postulado'), "
    "span:has-text('Ya aplicaste'), "
    "span:has-text('Postulado'), "
    "[data-applied='true'], "
    ".cv-applied"
)
# "Preguntas de selección" — a second form that appears after the first submit
SELECTION_QUESTIONS_SELECTOR = "text=Preguntas de selección, h1:has-text('Preguntas de selección')"
AUTH_ERROR_SELECTOR = "a[href*='/login'], .login-required"
WEBPUSH_POPUP_SELECTOR = "#pop-up-webpush-sub"
WEBPUSH_BACKDROP_SELECTOR = "#pop-up-webpush-background"
WEBPUSH_DISMISS_SELECTORS = [
    "#pop-up-webpush-sub button:has-text('Ahora no')",
    "#pop-up-webpush-sub a:has-text('Ahora no')",
    "text=Ahora no",
]


@register
class ComputrabajoApplier(BaseApplier):
    platform_name = "computrabajo"
    cv_profile_name = "local"

    def _do_apply(self, application: Application, job: Job) -> ApplicationResult:
        if not self.session_manager.has_cookies("computrabajo"):
            raise AuthExpired("No cookies for computrabajo")

        resume_step = self._get_resume_step(application)
        cv_profile_name = self._get_application_cv_profile_name(application)
        pdf_path = get_pdf_path(cv_profile_name)
        filler = FormFiller(cv_profile_name)
        has_form: bool = False

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless(), args=CHROMIUM_ARGS)
            context = browser.new_context(
                viewport={"width": 1280, "height": 900},
                locale="es-AR",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
            )
            self.session_manager.inject_into_playwright(context, "computrabajo")
            page = context.new_page()
            stealth_sync(page)

            try:
                if resume_step <= 0:
                    self._save_checkpoint(application, 0, "navigate")
                    page.goto(job.url, wait_until="load", timeout=30_000)
                    page.wait_for_timeout(1000)  # let JS redirects settle
                    _dismiss_webpush_popup(page)
                    # Detect redirect to search results or 403/404 → job expired
                    if _is_job_unavailable(page, job.url):
                        browser.close()
                        return ApplicationResult(
                            False, ApplicationStatus.FAILED.value,
                            "Job no longer available (URL redirected to search results)",
                        )
                    _check_auth(page)
                    _check_captcha(page)

                if resume_step <= 1:
                    self._save_checkpoint(application, 1, "click_apply")
                    try:
                        _dismiss_webpush_popup(page)
                        if _verify_success(page):
                            browser.close()
                            return ApplicationResult(True, ApplicationStatus.APPLIED.value)
                        url_before = page.url
                        btn = page.locator(APPLY_BTN_SELECTOR).first
                        # Save href before clicking — CT's JS sometimes intercepts the click
                        # and silently fails (bot-detection). If click doesn't navigate,
                        # we fall back to navigating directly to the match URL.
                        # CT stores the apply URL in href or data-href-offer-apply or data-apply-link.
                        btn_href = (
                            btn.get_attribute("href") or
                            btn.get_attribute("data-href-offer-apply") or
                            btn.get_attribute("data-apply-link") or
                            ""
                        )
                        # If still empty, find the Postularme anchor more specifically
                        if not btn_href:
                            postularme_a = page.locator("a:has-text('Postularme')").first
                            if postularme_a.count() > 0:
                                btn_href = postularme_a.get_attribute("href") or ""
                        btn.click(timeout=8_000, force=True)
                        page.wait_for_load_state("domcontentloaded", timeout=15_000)
                        _dismiss_webpush_popup(page)
                        _check_auth(page)
                        _check_captcha(page)
                        # If still on the same listing page → JS intercepted click
                        if page.url == url_before or page.url.rstrip("/") == url_before.rstrip("/"):
                            page.wait_for_timeout(1500)  # wait for modal/toast to appear
                            # Try clicking a "Confirmar" button from the modal
                            try:
                                confirm = page.locator(CONFIRM_SELECTOR).first
                                if confirm.is_visible(timeout=2000):
                                    confirm.click(timeout=5000)
                                    page.wait_for_load_state("domcontentloaded", timeout=15_000)
                                    page.wait_for_timeout(1000)
                            except Exception:
                                pass
                            if _verify_success(page) or _is_already_applied(page):
                                browser.close()
                                return ApplicationResult(True, ApplicationStatus.APPLIED.value)
                            # JS intercepted click but didn't navigate → go directly to match URL
                            if btn_href and "candidato" in btn_href:
                                logger.info("computrabajo.direct_apply_fallback",
                                            job_id=job.id, url=btn_href[:80])
                                page.goto(btn_href, wait_until="load", timeout=30_000)
                                page.wait_for_timeout(1500)
                                _check_auth(page)
                                if _verify_success(page) or _is_already_applied(page):
                                    browser.close()
                                    return ApplicationResult(True, ApplicationStatus.APPLIED.value)
                                # Landed on KillerQuestions page (/candidate/kq)
                                if _is_on_selection_questions(page) or "/kq" in page.url:
                                    logger.info("computrabajo.direct_apply_kq", job_id=job.id)
                                    _fill_selection_questions(page, filler)
                                    _submit(page, job.id)
                                    page.wait_for_load_state("domcontentloaded", timeout=15_000)
                                    page.wait_for_timeout(1000)
                                    if _verify_success(page) or _is_already_applied(page):
                                        browser.close()
                                        return ApplicationResult(True, ApplicationStatus.APPLIED.value)
                    except PWTimeout:
                        screenshot = capture(page, f"no_apply_btn_{job.id}")
                        browser.close()
                        return ApplicationResult(
                            False,
                            ApplicationStatus.FAILED.value,
                            "Postularme button not found",
                            screenshot_path=screenshot,
                        )

                if resume_step <= 2:
                    self._save_checkpoint(application, 2, "fill_form")
                    apply_form_inputs = page.locator(
                        "form textarea:visible, "
                        "form input[type='text']:visible, "
                        "form input[type='file']:visible"
                    ).count()
                    has_form = apply_form_inputs > 0 or _is_on_selection_questions(page)
                    if has_form:
                        _fill_form(page, filler, pdf_path)

                if resume_step <= 3:
                    self._save_checkpoint(application, 3, "submit")
                    if _verify_success(page):
                        browser.close()
                        return ApplicationResult(True, ApplicationStatus.APPLIED.value)
                    if not has_form and not _is_on_selection_questions(page):
                        screenshot = capture(page, f"no_form_no_confirm_{job.id}")
                        browser.close()
                        return ApplicationResult(
                            False, ApplicationStatus.FAILED.value,
                            "No form found and could not confirm instant apply",
                            screenshot_path=screenshot,
                        )
                    _submit(page, job.id)

                # Step 3.5: "Preguntas de selección" — second form after initial submit
                if resume_step <= 35:
                    self._save_checkpoint(application, 35, "selection_questions")
                    if _is_on_selection_questions(page):
                        logger.info("computrabajo.selection_questions_detected", job_id=job.id)
                        _fill_selection_questions(page, filler)
                        _submit(page, job.id)

                if resume_step <= 4:
                    self._save_checkpoint(application, 4, "verify")
                    success = _verify_success(page)
                    if not success:
                        # One more check: if we're still on selection questions, fill again
                        if _is_on_selection_questions(page):
                            logger.warning("computrabajo.selection_questions_still_visible", job_id=job.id)
                            _fill_selection_questions(page, filler)
                            _submit(page, job.id)
                            page.wait_for_load_state("domcontentloaded", timeout=15_000)
                            success = _verify_success(page)
                        if not success:
                            screenshot = capture(page, f"unconfirmed_{job.id}")
                            browser.close()
                            return ApplicationResult(
                                False,
                                ApplicationStatus.FAILED.value,
                                "Could not confirm successful submission",
                                screenshot_path=screenshot,
                            )

                browser.close()
                return ApplicationResult(True, ApplicationStatus.APPLIED.value)

            except (CaptchaDetected, AuthExpired):
                capture(page, f"auth_captcha_{self.platform_name}_{job.id}")
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
    if page.locator(AUTH_ERROR_SELECTOR).count() > 0:
        raise AuthExpired(f"Session not authenticated at {page.url}")


def _check_captcha(page):
    captcha_signals = [
        "iframe[src*='captcha']",
        "iframe[src*='recaptcha']",
        "#challenge-running",
        "[data-ray]",
    ]
    for selector in captcha_signals:
        if page.locator(selector).count() > 0:
            raise CaptchaDetected(f"CAPTCHA at {page.url}")


def _dismiss_webpush_popup(page):
    try:
        popup = page.locator(WEBPUSH_POPUP_SELECTOR)
        if popup.count() == 0 or not popup.first.is_visible(timeout=1_000):
            return
    except Exception:
        return

    try:
        dismissed = False
        for selector in WEBPUSH_DISMISS_SELECTORS:
            loc = page.locator(selector)
            if loc.count() == 0:
                continue
            try:
                loc.first.click(timeout=3_000, force=True)
                page.wait_for_timeout(500)
                dismissed = True
                break
            except Exception:
                continue
        if not dismissed:
            raise RuntimeError("dismiss button not found")
    except Exception:
        try:
            page.evaluate(
                """
                () => {
                    for (const selector of ['#pop-up-webpush-sub', '#pop-up-webpush-background']) {
                        const el = document.querySelector(selector);
                        if (el) el.remove();
                    }
                }
                """
            )
        except Exception:
            return

    try:
        page.locator(WEBPUSH_POPUP_SELECTOR).first.wait_for(state="hidden", timeout=3_000)
    except Exception:
        pass
    try:
        page.locator(WEBPUSH_BACKDROP_SELECTOR).first.wait_for(state="hidden", timeout=3_000)
    except Exception:
        pass


def _fill_form(page, filler: FormFiller, pdf_path: Path) -> None:
    # CV PDF attachment
    file_inputs = page.locator("input[type='file']")
    if file_inputs.count() > 0:
        file_inputs.first.set_input_files(str(pdf_path))

    # Text / number / email / tel fields
    inputs = page.locator(
        "input[type='text']:visible, input[type='number']:visible, "
        "input[type='email']:visible, input[type='tel']:visible"
    )
    for i in range(inputs.count()):
        inp = inputs.nth(i)
        label = _get_label(page, inp)
        if not label:
            continue
        if inp.input_value():
            continue
        field_type = inp.get_attribute("type") or "text"
        answer = filler.fill(label, field_type=field_type)
        if answer:
            inp.fill(answer)

    # Textarea fields
    textareas = page.locator("textarea:visible")
    for i in range(textareas.count()):
        ta = textareas.nth(i)
        try:
            if ta.input_value().strip():
                continue
        except Exception:
            pass
        label = _get_closest_label(page, ta)
        if not label:
            continue
        answer = filler.fill(label, field_type="text")
        if answer:
            ta.fill(str(answer)[:500])

    # Select / dropdown fields
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
                    logger.debug("computrabajo.select_filled", label=label, value=matched)
                except Exception:
                    pass

    # Radio button groups (Sí/No, etc.)
    _fill_radio_groups(page, filler)

    # Checkboxes (terms & conditions, etc.)
    _fill_checkboxes(page, filler)


def _fill_radio_groups(page, filler: FormFiller) -> None:
    """Handle radio button groups (e.g., ¿Contás con movilidad propia? Sí/No)."""
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
        # Skip already-selected groups
        already_checked = page.evaluate(
            f"""() => !!document.querySelector('input[type="radio"][name="{name}"]:checked')"""
        )
        if already_checked:
            continue

        # Get question label (legend > paragraph > walk up)
        question = page.evaluate(f"""
            () => {{
                const radio = document.querySelector('input[type="radio"][name="{name}"]');
                if (!radio) return null;
                const fieldset = radio.closest('fieldset');
                if (fieldset) {{
                    const legend = fieldset.querySelector('legend');
                    if (legend) return legend.textContent.trim();
                }}
                // Walk up looking for a meaningful text block
                let el = radio.parentElement;
                for (let i = 0; i < 6; i++) {{
                    if (!el || el.tagName === 'FORM' || el.tagName === 'BODY') break;
                    // Find a paragraph or div that is sibling/ancestor with question text
                    const candidates = el.querySelectorAll('p, legend, label:not([for])');
                    for (const c of candidates) {{
                        const t = c.textContent.trim();
                        if (t.length > 5 && t.length < 300 && !t.includes('\\n')) return t;
                    }}
                    el = el.parentElement;
                }}
                return name;
            }}
        """) or name

        # Get available option labels
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

        is_required = bool(page.evaluate(
            f"""() => !!document.querySelector('input[type="radio"][name="{name}"][required]')"""
        ))

        try:
            answer = filler.fill(question, field_type="boolean", required=is_required)
            if not answer:
                continue
            answer_norm = answer.lower().strip()
            # Try to match answer to one of the options
            clicked = False
            for opt in options:
                opt_label = opt["label"].lower().strip()
                opt_value = opt["value"].lower().strip()
                if (answer_norm in ("sí", "si", "yes", "true", "1") and opt_label in ("sí", "si", "yes")):
                    _click_radio(page, name, opt["value"])
                    clicked = True
                    break
                elif (answer_norm in ("no", "false", "0") and opt_label == "no"):
                    _click_radio(page, name, opt["value"])
                    clicked = True
                    break
                elif opt_label == answer_norm or opt_value == answer_norm:
                    _click_radio(page, name, opt["value"])
                    clicked = True
                    break
            if not clicked and options:
                # Fallback: click first option
                logger.debug("radio.fallback_first_option", question=question, answer=answer)
                _click_radio(page, name, options[0]["value"])
        except Exception as e:
            logger.debug("computrabajo.radio_fill_error", question=question, error=str(e))


def _click_radio(page, name: str, value: str):
    try:
        page.locator(f"input[type='radio'][name='{name}'][value='{value}']").first.click(timeout=3_000)
    except Exception:
        # Fallback: click via JS
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
        placeholder = input_el.get_attribute("placeholder")
        if placeholder:
            return placeholder
        return input_el.get_attribute("name")
    except Exception:
        return None


def _get_closest_label(page, input_el) -> Optional[str]:
    """Walk up the DOM to find the closest descriptive label for an input element."""
    try:
        label = page.evaluate("""
            (el) => {
                if (el.id) {
                    const lbl = document.querySelector(`label[for="${el.id}"]`);
                    if (lbl) return lbl.textContent.trim();
                }
                if (el.getAttribute('placeholder')) return el.getAttribute('placeholder');
                // Walk up: look for preceding sibling text or parent label/p
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
        return label
    except Exception:
        return None


def _fill_checkboxes(page, filler: FormFiller) -> None:
    """
    Handle checkbox fields. Terms & conditions checkboxes are auto-checked.
    Other checkboxes (e.g., availability, consent) are answered via LLM.
    """
    try:
        checkboxes = page.locator("input[type='checkbox']:visible:not(:checked)")
        count = checkboxes.count()
        for i in range(count):
            cb = checkboxes.nth(i)
            try:
                label = _get_closest_label(page, cb) or cb.get_attribute("name") or ""
                label_lower = label.lower()
                # Auto-check terms/conditions/privacy without asking LLM
                auto_check_keywords = ("término", "termino", "condicion", "condición",
                                       "privacidad", "acepto", "acepta", "política",
                                       "politica", "terms", "privacy", "agree")
                if any(kw in label_lower for kw in auto_check_keywords):
                    cb.check(timeout=3_000)
                    logger.debug("computrabajo.checkbox_auto_checked", label=label[:60])
                    continue
                # Ask LLM for other checkboxes
                if label:
                    answer = filler.fill(label, field_type="boolean")
                    if answer and answer.lower().strip() in ("sí", "si", "yes", "true", "1"):
                        cb.check(timeout=3_000)
                        logger.debug("computrabajo.checkbox_checked", label=label[:60])
            except Exception as e:
                logger.debug("computrabajo.checkbox_fill_error", error=str(e))
    except Exception as e:
        logger.debug("computrabajo.fill_checkboxes_error", error=str(e))


def _submit(page, job_id: int):
    if _verify_success(page):
        logger.info("computrabajo.already_applied", job_id=job_id)
        return

    try:
        btn = page.locator(CONFIRM_SELECTOR).first
        btn.click(timeout=8_000)
        page.wait_for_load_state("domcontentloaded", timeout=20_000)
        logger.info("computrabajo.submitted", job_id=job_id)
    except PWTimeout:
        # Fallback: JS click on the innermost element matching submit-like text
        clicked = page.evaluate("""
            () => {
                const texts = ['Enviar mi CV', 'Enviar CV', 'Enviar postulación', 'Confirmar'];
                const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                let node;
                while (node = walker.nextNode()) {
                    const txt = node.textContent.trim();
                    if (texts.includes(txt)) {
                        node.parentElement.click();
                        return true;
                    }
                }
                // Last resort: click any submit button
                const submit = document.querySelector('button[type=submit], input[type=submit]');
                if (submit) { submit.click(); return true; }
                return false;
            }
        """)
        if clicked:
            page.wait_for_load_state("domcontentloaded", timeout=20_000)
            logger.info("computrabajo.submitted_via_js", job_id=job_id)
        else:
            if _verify_success(page):
                logger.info("computrabajo.submitted_after_timeout", job_id=job_id)
                return
            raise Exception("Submit failed: no submit button found")


def _verify_success(page) -> bool:
    for selector in SUCCESS_SELECTOR.split(", "):
        try:
            if page.locator(selector).count() > 0:
                return True
        except Exception:
            continue
    return _is_already_applied(page)


def _is_already_applied(page) -> bool:
    """Check if CT changed the apply button/page state to indicate already-applied."""
    for selector in ALREADY_APPLIED_SELECTOR.split(", "):
        try:
            if page.locator(selector.strip()).count() > 0:
                return True
        except Exception:
            continue
    return False


def _is_job_unavailable(page, original_url: str) -> bool:
    """
    Detect if a job offer is no longer available.
    Covers: 403/404 pages, redirect to search results, expired offer pages.
    """
    current_url = page.url
    title = page.title().lower()

    # HTTP error pages
    if "403" in title or "404" in title or "forbidden" in title or "not found" in title:
        return True
    # Redirect from specific offer to search results
    if "/oferta-de-trabajo-de-" in original_url and "/oferta-de-trabajo-de-" not in current_url:
        return True
    if "/trabajo-de-" in current_url or "/empleos-de-" in current_url:
        return True
    # CT sometimes shows an error overlay/text
    try:
        if page.locator("text=oferta no disponible, text=oferta vencida, text=no existe").count() > 0:
            return True
    except Exception:
        pass
    return False


def _is_on_selection_questions(page) -> bool:
    """Detect Computrabajo's second-step 'Preguntas de selección' page."""
    try:
        for selector in SELECTION_QUESTIONS_SELECTOR.split(", "):
            if page.locator(selector.strip()).count() > 0:
                return True
    except Exception:
        pass
    return False


def _fill_selection_questions(page, filler: FormFiller) -> None:
    """
    Fill the 'Preguntas de selección' page.
    Each question is a textarea preceded by a visible text label (p, span, or div).
    We use JS to get the question text closest to each textarea.
    """
    try:
        textareas = page.locator("textarea:visible")
        count = textareas.count()
        for i in range(count):
            ta = textareas.nth(i)
            # Get current value — skip if already filled
            try:
                current = ta.input_value()
                if current and current.strip():
                    continue
            except Exception:
                pass

            # Extract the question label via JS: look for the closest preceding text node
            label = page.evaluate("""
                (el) => {
                    // Try label[for=id]
                    if (el.id) {
                        const lbl = document.querySelector(`label[for="${el.id}"]`);
                        if (lbl) return lbl.textContent.trim();
                    }
                    // Walk up: look for p, span, label, h3, h4 siblings or parent text
                    let parent = el.parentElement;
                    for (let i = 0; i < 5; i++) {
                        if (!parent || parent.tagName === 'FORM') break;
                        // Check siblings before this element
                        for (const sib of parent.children) {
                            if (sib === el || sib.contains(el)) break;
                            const t = sib.textContent.trim();
                            if (t.length > 3 && t.length < 300) return t;
                        }
                        // Check parent's own text (excluding children)
                        const direct = Array.from(parent.childNodes)
                            .filter(n => n.nodeType === 3)
                            .map(n => n.textContent.trim())
                            .filter(t => t.length > 3)
                            .join(' ');
                        if (direct.length > 3) return direct;
                        parent = parent.parentElement;
                    }
                    return el.getAttribute('placeholder') || el.getAttribute('name') || null;
                }
            """, ta.element_handle())

            if not label:
                continue

            try:
                # Treat as required — CT KQ questions must be answered or we can't submit
                answer = filler.fill(label, field_type="text", required=True)
                if answer:
                    ta.fill(str(answer)[:500])
            except Exception as e:
                logger.debug("computrabajo.selection_question_fill_error", label=label, error=str(e))

    except Exception as e:
        logger.warning("computrabajo.fill_selection_questions_error", error=str(e))
