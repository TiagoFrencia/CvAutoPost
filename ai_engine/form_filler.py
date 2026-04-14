"""
Form Filler — answers form fields during job applications.

Priority order (cheapest first):
  1. answers.yaml exact key match → return value directly (zero LLM cost)
  2. answers.yaml fuzzy key match (normalised label)
  3. LLM with CV context + profile_context.yaml → always returns an answer (never blocks)

The LLM is instructed to ALWAYS provide an answer using CV data + profile context.
OrphanQuestion is kept for backward compatibility but never raised.
Successful LLM answers ≤150 chars are auto-saved to answers.yaml so the next
identical question is free.
"""
import json
import re
from pathlib import Path
from typing import Optional

import requests
import yaml
import structlog

from ai_engine.cv_loader import get_cv
from core.config import settings

logger = structlog.get_logger()

ANSWERS_YAML_PATH = settings.data_dir / "answers.yaml"
PROFILE_CONTEXT_PATH = settings.data_dir / "profile_context.yaml"

FORM_SYSTEM_PROMPT = """Eres un asistente que completa formularios de postulación laboral en nombre de un candidato.
Usás los datos del CV JSON para responder. Sé conciso y directo.

REGLAS:
1. SIEMPRE respondé con un valor — nunca digas que no podés responder.
2. Si la pregunta tiene una respuesta clara en el CV → usala.
3. Si la pregunta es ambigua o no está en el CV → inferí la respuesta más razonable basándote en el perfil del candidato.
4. Para preguntas de opción múltiple con opciones listadas → elegí la opción cuyo TEXTO sea más cercano a la realidad del candidato. Respondé con el texto exacto de la opción.
5. Para preguntas booleanas (sí/no) → respondé solo "Sí" o "No".
6. Respondé en el idioma de la pregunta.
7. Para texto libre → sé breve (1-3 oraciones máximo).
8. Nunca inventes datos falsos verificables (documentos, títulos inexistentes, referencias, etc.).

Respondé SIEMPRE con JSON válido:
{"answer": "<respuesta>"}"""

COVER_LETTER_SYSTEM_PROMPT = """Eres un redactor experto en cartas de presentación para postulaciones laborales.
Escribís en nombre del candidato. Sé directo, auténtico y específico para la oferta.

REGLAS:
1. La carta debe ser de 3 párrafos cortos (máximo 180 palabras en total).
2. Párrafo 1: quién sos y por qué esta oferta específica te interesa (mencioná la empresa y el rol).
3. Párrafo 2: el proyecto o experiencia más relevante del CV que demuestra que podés hacer el trabajo.
4. Párrafo 3: disponibilidad y llamada a la acción.
5. NO uses frases genéricas ("soy una persona proactiva", "trabajo en equipo"). Sé concreto.
6. Escribí en el idioma de la oferta (si tiene inglés en el título/descripción → inglés; si no → español).
7. Respondé SIEMPRE con JSON válido: {"answer": "<carta completa>"}"""

# Cover letter field label patterns — triggers personalized generation when job context is set
_COVER_LETTER_LABELS = {
    "cover_letter", "carta_de_presentacion", "carta de presentacion",
    "carta_presentacion", "presentacion", "motivacion", "por qué aplicás",
    "por que aplicas", "why are you applying", "cover letter",
    "motivación", "why do you want to work", "why this role",
    "why this company", "por que queres trabajar",
}


class OrphanQuestion(Exception):
    """Kept for backward compatibility. No longer raised by FormFiller."""
    def __init__(self, question_text: str):
        self.question_text = question_text
        super().__init__(f"Orphan question: {question_text}")


class FormFiller:
    def __init__(self, cv_profile_name: str = "remoto"):
        self.cv_profile_name = cv_profile_name
        self.cv_data = get_cv(cv_profile_name)
        self._answers_cache: Optional[dict] = None
        self._profile_context: Optional[str] = None
        # Job context — set via set_job_context() to enable personalized cover letters
        self._job_title: Optional[str] = None
        self._job_company: Optional[str] = None
        self._job_description: Optional[str] = None

    def set_job_context(self, title: str, company: Optional[str], description: Optional[str]) -> None:
        """
        Provide the current job's details so personalized cover letters can be generated.
        Call this once per application before the first fill() call.
        """
        self._job_title = title
        self._job_company = company or ""
        self._job_description = (description or "")[:2000]

    @property
    def answers(self) -> dict:
        if self._answers_cache is None:
            self._answers_cache = _load_answers_yaml()
        return self._answers_cache

    def fill(self, field_label: str, field_type: str = "text", required: bool = True) -> Optional[str]:
        """
        Return the answer for a form field. Never raises OrphanQuestion.
        - field_label: the label/question text as it appears in the form
        - field_type: 'text', 'number', 'boolean', 'dropdown', 'file'
        - required: kept for API compatibility, no longer affects behavior
        Returns None only when the LLM call fails entirely.
        """
        # Step 0: personalized cover letter — bypass YAML when job context is available
        if self._job_title and _is_cover_letter_field(field_label):
            logger.debug("form_filler.personalized_cover_letter", field=field_label)
            return self._generate_personalized_cover_letter()

        # Step 1: exact YAML key match
        answer = _yaml_exact_match(self.answers, field_label)
        if answer is not None:
            logger.debug("form_filler.yaml_hit", field=field_label)
            return str(answer)

        # Step 2: normalised fuzzy key match
        answer = _yaml_fuzzy_match(self.answers, field_label)
        if answer is not None:
            logger.debug("form_filler.yaml_fuzzy_hit", field=field_label)
            return str(answer)

        # Step 3: LLM — always returns an answer
        logger.debug("form_filler.llm_call", field=field_label)
        answer = self._ask_llm(field_label, field_type)
        if answer:
            self._auto_save(field_label, answer)
        return answer

    @property
    def profile_context(self) -> str:
        """Narrative context from profile_context.yaml, scoped to the active CV profile."""
        if self._profile_context is None:
            self._profile_context = _load_profile_context(self.cv_profile_name)
        return self._profile_context

    def _generate_personalized_cover_letter(self) -> Optional[str]:
        """Generate a cover letter tailored to the specific job stored in job context."""
        cv_summary = json.dumps(self.cv_data, ensure_ascii=False)[:2000]
        user_prompt = (
            f"CV del candidato (JSON):\n{cv_summary}\n\n"
            f"Contexto adicional:\n{self.profile_context}\n\n"
            f"Oferta de trabajo:\n"
            f"  Título: {self._job_title}\n"
            f"  Empresa: {self._job_company}\n"
            f"  Descripción: {self._job_description}\n\n"
            f"Escribí una carta de presentación personalizada para esta oferta."
        )
        logger.info(
            "form_filler.generating_cover_letter",
            job=self._job_title,
            company=self._job_company,
        )
        answer = self._ask_ollama_with_prompt(COVER_LETTER_SYSTEM_PROMPT, user_prompt, "cover_letter")
        if answer is None:
            answer = self._ask_gemini_with_prompt(COVER_LETTER_SYSTEM_PROMPT, user_prompt, "cover_letter")
        return answer

    def _ask_llm(self, field_label: str, field_type: str) -> Optional[str]:
        cv_summary = json.dumps(self.cv_data, ensure_ascii=False)[:2000]
        user_prompt = (
            f"CV del candidato (JSON):\n{cv_summary}\n\n"
            f"Contexto adicional del candidato:\n{self.profile_context}\n\n"
            f"Campo del formulario: \"{field_label}\"\n"
            f"Tipo de campo: {field_type}"
        )

        # Primary: Ollama (local, zero cost)
        answer = self._ask_ollama(user_prompt, field_label)
        if answer is not None:
            return answer

        # Fallback: Gemini (when Ollama is unreachable or times out)
        logger.info("form_filler.ollama_failed_using_gemini", field=field_label)
        return self._ask_gemini(user_prompt, field_label)

    def _ask_ollama(self, user_prompt: str, field_label: str) -> Optional[str]:
        return self._ask_ollama_with_prompt(FORM_SYSTEM_PROMPT, user_prompt, field_label)

    def _ask_gemini(self, user_prompt: str, field_label: str) -> Optional[str]:
        return self._ask_gemini_with_prompt(FORM_SYSTEM_PROMPT, user_prompt, field_label)

    def _ask_ollama_with_prompt(self, system_prompt: str, user_prompt: str, field_label: str) -> Optional[str]:
        payload = {
            "model": settings.ollama_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.1},
        }
        try:
            resp = requests.post(
                f"{settings.ollama_url}/api/chat",
                json=payload,
                timeout=60,
            )
            resp.raise_for_status()
            raw_text = resp.json()["message"]["content"]
            return _parse_fill_response(raw_text, field_label)
        except Exception as e:
            logger.warning("form_filler.ollama_error", field=field_label, error=str(e))
            return None

    def _ask_gemini_with_prompt(self, system_prompt: str, user_prompt: str, field_label: str) -> Optional[str]:
        try:
            import google.generativeai as genai
            genai.configure(api_key=settings.gemini_api_key)
            model = genai.GenerativeModel(
                model_name="gemini-2.5-flash",
                system_instruction=system_prompt,
            )
            response = model.generate_content(user_prompt)
            return _parse_fill_response(response.text, field_label)
        except Exception as e:
            logger.error("form_filler.gemini_error", field=field_label, error=str(e))
            return None

    def _auto_save(self, field_label: str, answer: str) -> None:
        """
        Persist LLM answer to answers.yaml so next identical question is free.
        Only saves short factual answers (≤150 chars) — skips essays/cover letters.
        """
        if len(answer) > 150:
            return
        key = _normalise_key(field_label)
        # Don't overwrite existing entries
        if key in self.answers or field_label in self.answers:
            return
        try:
            current = _load_answers_yaml()
            if key in current or field_label in current:
                return
            current[key] = answer
            ANSWERS_YAML_PATH.write_text(
                yaml.dump(current, allow_unicode=True, default_flow_style=False, sort_keys=True),
                encoding="utf-8",
            )
            # Invalidate cache so next call picks up the new entry
            self._answers_cache = None
            logger.info("form_filler.answer_autosaved", field=field_label, key=key, answer=answer[:60])
        except Exception as e:
            logger.debug("form_filler.autosave_error", field=field_label, error=str(e))


# ── Helpers ──────────────────────────────────────────────────────────────────

def _is_cover_letter_field(label: str) -> bool:
    """Return True if the field label indicates a cover letter / motivation question."""
    label_norm = _normalise_key(label)
    return any(_normalise_key(pattern) in label_norm or label_norm in _normalise_key(pattern)
               for pattern in _COVER_LETTER_LABELS)


def _load_profile_context(cv_profile_name: str) -> str:
    """
    Load profile_context.yaml and return a formatted string with the sections
    relevant to the given CV profile (remoto or local), plus the general section.
    Returns an empty string if the file is missing.
    """
    if not PROFILE_CONTEXT_PATH.exists():
        logger.warning("form_filler.no_profile_context", path=str(PROFILE_CONTEXT_PATH))
        return ""
    try:
        data = yaml.safe_load(PROFILE_CONTEXT_PATH.read_text(encoding="utf-8")) or {}
    except Exception as e:
        logger.warning("form_filler.profile_context_load_error", error=str(e))
        return ""

    sections = []

    # Profile-specific section (remoto or local)
    profile_data = data.get(cv_profile_name, {})
    if profile_data:
        sections.append(f"[Perfil {cv_profile_name}]")
        sections.append(_flatten_yaml_section(profile_data))

    # General section (applies to both profiles)
    general = data.get("general", {})
    if general:
        sections.append("[General]")
        sections.append(_flatten_yaml_section(general))

    return "\n".join(sections)


def _flatten_yaml_section(data: dict, indent: int = 0) -> str:
    """Recursively flatten a YAML dict into readable key: value lines."""
    lines = []
    prefix = "  " * indent
    for key, value in data.items():
        if isinstance(value, dict):
            lines.append(f"{prefix}{key}:")
            lines.append(_flatten_yaml_section(value, indent + 1))
        else:
            # Collapse multiline strings to a single line for the prompt
            val_str = str(value).replace("\n", " ").strip()
            lines.append(f"{prefix}{key}: {val_str}")
    return "\n".join(lines)


def _load_answers_yaml() -> dict:
    path = ANSWERS_YAML_PATH
    if not path.exists():
        logger.warning("form_filler.no_yaml", path=str(path))
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _normalise_key(text: str) -> str:
    """Lowercase, strip, replace spaces/hyphens/special chars with underscores."""
    return re.sub(r"[\s\-/¿?¡!:,\.]+", "_", text.lower().strip()).strip("_")


def _yaml_exact_match(answers: dict, label: str) -> Optional[str]:
    v = answers.get(label)
    if v is not None:
        return v
    return answers.get(_normalise_key(label))


def _yaml_fuzzy_match(answers: dict, label: str) -> Optional[str]:
    """Try substring matching: if any YAML key appears in the label or vice versa."""
    label_norm = _normalise_key(label)
    for key in answers:
        key_norm = _normalise_key(str(key))
        if len(key_norm) < 4:
            continue
        if key_norm in label_norm or label_norm in key_norm:
            return answers[key]
    return None


def _parse_fill_response(raw_text: str, field_label: str) -> Optional[str]:
    text = re.sub(r"```(?:json)?", "", raw_text).strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        logger.warning("form_filler.no_json", field=field_label, raw=raw_text[:100])
        return None

    try:
        data = json.loads(match.group())
    except json.JSONDecodeError:
        return None

    answer = data.get("answer")
    if answer is None:
        return None
    return str(answer).strip() if str(answer).strip() else None
