"""
Job Matcher — evaluates each PENDING job against a CV profile using Gemma 4 (local via Ollama).

Flow per job:
  1. Pre-filter (legal keywords) — skip without LLM call if blocked
  2. LLM call via Ollama (primary) or Gemini (fallback if Ollama unreachable)
  3. Pydantic validation of JSON response
  4. Write MatchResult to DB
  5. Update Job.status (SCORED → AUTO_APPLY / REVIEW_SCORE / SKIPPED)
"""
import json
import re
from typing import Optional

import structlog
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.orm import Session
from sqlalchemy import or_

from ai_engine.context_cache import maybe_cached_model
from ai_engine.cv_loader import get_cv
from core.config import settings
from core.enums import JobStatus, ApplicationStatus
from core.models import Job, MatchResult, CVProfile, Application

logger = structlog.get_logger()

_GEMINI_FALLBACK_ERRORS = ("connection", "refused", "unreachable", "timeout")

# Platforms that have no applier — jobs are scored but never queued for application.
# WeWorkRemotely listings redirect to external sites; there's no in-platform apply flow.
_PLATFORMS_WITHOUT_APPLIER = {"weworkremotely"}

# ── Pre-filter keyword lists ─────────────────────────────────────────────────

# Any of these in title or description → SKIPPED immediately, no LLM call
LEGAL_REJECT_KEYWORDS = [
    "w-2", "w2 employee", "us citizen", "u.s. citizen",
    "green card", "security clearance", "clearance required",
    "us person", "must be authorized to work in the us",
    "authorized to work in the united states",
]

# Any of these in the JOB TITLE → SKIPPED without LLM call (saves Gemini tokens)
# These are senior/management roles a 0-experience junior cannot get
TITLE_REJECT_KEYWORDS = [
    "senior", " sr ", "sr.", " lead ", "tech lead", "technical lead",
    "principal", "staff engineer", "manager", "director", "architect",
    "head of", "vp ", "vp,", "cto", "cpo", "ciso",
]

# Any of these → bonus flag passed to LLM (no skip)
LEGAL_BOOST_KEYWORDS = [
    "latam", "latin america", "argentina", "contractor", "b2b",
    "1099", "gmt-3", "remote from anywhere", "worldwide", "global",
]

# ── System prompt ────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """Eres un reclutador senior especializado en tecnología y mercado laboral argentino.
El candidato es un profesional basado en Argentina que exporta servicios de forma remota.

Tu tarea es evaluar si el candidato es apto para la oferta de trabajo.

PERFIL DEL CANDIDATO (resumen):
- Nivel: Junior. 0 años de experiencia IT remunerada formal.
- Proyectos propios desde 2024 equivalentes a ~1 año de práctica.
- Stack principal: Java, Spring Boot, React, Python, FastAPI.

REGLAS (aplicar en orden):
1. Responde ÚNICAMENTE con un JSON válido. Sin texto adicional, sin markdown, sin bloques de código.
2. El campo "score" es un entero de 0 a 100.
3. REGLAS DE SENIORITY — aplicar ANTES de calcular score:
   - Si el título contiene "Senior", "Sr.", "Lead", "Principal", "Staff", "Manager",
     "Director", "Architect", "Head of", "VP", "CTO", "Tech Lead" o equivalentes
     → score DEBE ser ≤ 45 y auto_apply = false.
   - Si la oferta exige más de 2 años de experiencia comprobable en IT
     → penalizar 25 puntos (el candidato tiene 0 años formales).
   - Si exige más de 3 años → score DEBE ser ≤ 50 y auto_apply = false.
4. El campo "auto_apply" es true SOLO si:
   - score >= 80
   - La oferta es de nivel Junior, Trainee, Entry-level, Semi-Senior (primer año) o sin seniority explícito
   - No viola ninguna regla del punto 3
5. Si la oferta está en español, suma 5 puntos (el candidato es nativo).
6. Si la modalidad es remota y el candidato puede trabajar remoto, suma 5 puntos.
7. VIABILIDAD LEGAL:
   - Si exige "W-2", "US Citizen", "Green Card" o "Security Clearance" → score DEBE ser < 40, legal_viability = "blocked"
   - Si acepta "Contractor", "B2B", "1099", menciona "LATAM", "Argentina" o "GMT-3" → suma 10 puntos, legal_viability = "viable"
   - En caso de duda → legal_viability = "restricted"

FORMATO DE RESPUESTA (exactamente este JSON, sin nada más):
{
  "score": <int 0-100>,
  "match_reason": "<1-2 oraciones explicando el score>",
  "auto_apply": <true|false>,
  "missing_skills": ["<skill1>", "<skill2>"],
  "risk_flags": ["<flag1>"],
  "legal_viability": "<viable|restricted|blocked>"
}"""

# ── Pydantic response schema ─────────────────────────────────────────────────

class MatchResponse(BaseModel):
    score: int = Field(ge=0, le=100)
    match_reason: str
    auto_apply: bool
    missing_skills: list[str] = []
    risk_flags: list[str] = []
    legal_viability: str = "viable"


# ── Main matcher class ───────────────────────────────────────────────────────

class JobMatcher:
    def __init__(self, db: Session, cv_profile_name: str = "remoto"):
        self.db = db
        self.cv_profile_name = cv_profile_name
        self.cv_data = get_cv(cv_profile_name)
        self.cv_profile = db.query(CVProfile).filter_by(name=cv_profile_name).first()
        if not self.cv_profile:
            raise ValueError(f"CVProfile '{cv_profile_name}' not in DB. Run: python main.py seed")
        # Cache: platform_id → platform_name
        self._platform_name_cache: dict[int, str] = {}

    def _get_platform_name(self, platform_id: int) -> str:
        if platform_id not in self._platform_name_cache:
            from core.models import Platform
            p = self.db.get(Platform, platform_id)
            self._platform_name_cache[platform_id] = p.name if p else ""
        return self._platform_name_cache[platform_id]

    def run_batch(
        self,
        platform_ids: list[int] = None,
        modalities: list[str] = None,
        include_null_modality: bool = False,
    ) -> dict:
        """
        Score PENDING jobs. If platform_ids is given, only jobs from those platforms
        are scored (used by pipeline to separate remoto vs local CV profiles).
        """
        query = self.db.query(Job).filter_by(status=JobStatus.PENDING.value)
        if platform_ids:
            query = query.filter(Job.platform_id.in_(platform_ids))
        if modalities:
            modality_filter = Job.modality.in_(modalities)
            if include_null_modality:
                modality_filter = or_(modality_filter, Job.modality.is_(None))
            query = query.filter(modality_filter)
        pending_jobs = query.all()

        logger.info("matcher.batch_start", count=len(pending_jobs), cv=self.cv_profile_name)

        stats = {"scored": 0, "skipped_prefilter": 0, "auto_apply": 0, "review": 0, "errors": 0}

        full_system_prompt = _build_system_prompt_with_cv(self.cv_data)
        self._system_prompt = full_system_prompt  # stored for Gemini fallback
        with maybe_cached_model(full_system_prompt, len(pending_jobs)) as model:
            for job in pending_jobs:
                try:
                    result = self._process_job(job, model)
                    if result == "prefilter_skip":
                        stats["skipped_prefilter"] += 1
                    elif result:
                        stats["scored"] += 1
                        if result.auto_apply:
                            stats["auto_apply"] += 1
                        elif result.score >= settings.score_review:
                            stats["review"] += 1
                except Exception as e:
                    logger.error("matcher.job_error", job_id=job.id, error=str(e))
                    stats["errors"] += 1

        logger.info("matcher.batch_done", **stats)
        return stats

    def _process_job(self, job: Job, model) -> Optional[MatchResponse] | str:
        """Score one job. Returns MatchResponse, 'prefilter_skip', or None on error."""
        # Step 1a: legal pre-filter (no LLM cost)
        if _is_legally_blocked(job.title, job.description):
            logger.debug("matcher.prefilter_blocked", job_id=job.id, title=job.title)
            job.status = JobStatus.SKIPPED.value
            self.db.commit()
            return "prefilter_skip"

        # Step 1b: seniority pre-filter — skip Senior/Lead titles without LLM call
        if _is_senior_title(job.title):
            logger.debug("matcher.prefilter_senior", job_id=job.id, title=job.title)
            job.status = JobStatus.SKIPPED.value
            self.db.commit()
            return "prefilter_skip"

        boost_hints = _get_boost_hints(job.title, job.description)

        # Step 2: LLM call (Ollama/Gemma4 primary, Gemini fallback if Ollama unreachable)
        prompt = _build_job_prompt(job, boost_hints)

        try:
            response = model.generate_content(prompt)
            raw_text = response.text
        except Exception as e:
            err = str(e).lower()
            if any(kw in err for kw in _GEMINI_FALLBACK_ERRORS):
                logger.warning("matcher.ollama_unreachable_using_gemini", job_id=job.id)
                raw_text = _gemini_generate(self._system_prompt, prompt)
                if raw_text is None:
                    return None
            else:
                logger.error("matcher.llm_error", job_id=job.id, error=str(e))
                return None

        # Step 3: parse + validate
        match_resp = _parse_response(raw_text, job.id)
        if match_resp is None:
            return None

        # Step 4: write to DB
        self._save_result(job, match_resp, raw_text)
        return match_resp

    def _save_result(self, job: Job, resp: MatchResponse, raw_text: str):
        match_result = MatchResult(
            job_id=job.id,
            cv_profile_id=self.cv_profile.id,
            score=resp.score,
            match_reason=resp.match_reason,
            auto_apply=resp.auto_apply,
            missing_skills=resp.missing_skills,
            risk_flags=resp.risk_flags,
            llm_response_raw={"text": raw_text},
        )
        self.db.add(match_result)

        # Update job status
        # Platforms with no applier (e.g. WeWorkRemotely) are scored for visibility
        # but never queued — their listings redirect to external sites.
        has_applier = self._get_platform_name(job.platform_id) not in _PLATFORMS_WITHOUT_APPLIER

        review_notify = False
        if resp.score < settings.score_review:
            job.status = JobStatus.SKIPPED.value
        elif resp.auto_apply and has_applier:
            job.status = JobStatus.AUTO_APPLY.value
            # Create Application record so the applier can pick it up
            self._create_application(job, resp.score)
        elif has_applier:
            job.status = JobStatus.REVIEW_SCORE.value
            review_notify = True
        else:
            # No applier available — mark as SKIPPED (scored but not actionable)
            job.status = JobStatus.SKIPPED.value
            logger.info("matcher.no_applier_skip", job_id=job.id, platform=self._get_platform_name(job.platform_id))

        self.db.commit()

        if review_notify:
            try:
                from services.telegram_bot import notify_review_score
                notify_review_score(job, match_result, self.cv_profile_name)
            except Exception as notify_err:
                logger.warning("matcher.review_notify_failed", job_id=job.id, error=str(notify_err))

        logger.info(
            "matcher.scored",
            job_id=job.id,
            score=resp.score,
            status=job.status,
            legal=resp.legal_viability,
        )

    def _create_application(self, job: Job, score: int):
        """Create an Application record with QUEUED status so the applier picks it up."""
        existing = self.db.query(Application).filter_by(
            job_id=job.id, cv_profile_id=self.cv_profile.id
        ).first()
        if existing:
            return  # already queued (e.g. after a re-run)
        application = Application(
            job_id=job.id,
            cv_profile_id=self.cv_profile.id,
            status=ApplicationStatus.QUEUED.value,
            priority_score=score,
            retry_count=0,
        )
        self.db.add(application)
        # commit is done by the caller (_save_result)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _build_system_prompt_with_cv(cv_data: dict) -> str:
    """Embed the CV JSON into the system prompt so both cached and direct models have it."""
    cv_json = json.dumps(cv_data, ensure_ascii=False, indent=2)
    return SYSTEM_PROMPT + f"\n\n--- CV DEL CANDIDATO (datos estructurados) ---\n{cv_json}"


def _normalise(text: str) -> str:
    return (text or "").lower()


def _is_legally_blocked(title: str, description: str) -> bool:
    combined = _normalise(title) + " " + _normalise(description)
    return any(kw in combined for kw in LEGAL_REJECT_KEYWORDS)


def _is_senior_title(title: str) -> bool:
    """Return True if the title clearly indicates a senior/lead/management role."""
    # Pad with spaces so " lead " matches at start/end too
    t = " " + _normalise(title) + " "
    return any(kw in t for kw in TITLE_REJECT_KEYWORDS)


def _get_boost_hints(title: str, description: str) -> list[str]:
    combined = _normalise(title) + " " + _normalise(description)
    return [kw for kw in LEGAL_BOOST_KEYWORDS if kw in combined]


def _build_job_prompt(job: Job, boost_hints: list[str]) -> str:
    lines = [
        "--- OFERTA DE TRABAJO ---",
        f"Título: {job.title}",
        f"Empresa: {job.company or 'N/A'}",
        f"Ubicación: {job.location or 'N/A'}",
        f"Modalidad: {job.modality or 'N/A'}",
        f"Descripción:\n{(job.description or '')[:3000]}",  # cap to avoid huge tokens
    ]
    if boost_hints:
        lines.append(f"\n[NOTA: La oferta contiene indicadores favorables: {', '.join(boost_hints)}]")
    return "\n".join(lines)


def _gemini_generate(system_prompt: str, user_prompt: str) -> Optional[str]:
    """
    Fallback: call Gemini when Ollama is unreachable.
    Returns the raw text response, or None on error.
    """
    try:
        import google.generativeai as genai
        genai.configure(api_key=settings.gemini_api_key)
        model = genai.GenerativeModel(
            model_name="gemini-2.5-flash",
            system_instruction=system_prompt,
        )
        response = model.generate_content(user_prompt)
        return response.text
    except Exception as e:
        logger.error("matcher.gemini_fallback_error", error=str(e))
        return None


def _parse_response(raw_text: str, job_id: int) -> Optional[MatchResponse]:
    """Extract and validate JSON from LLM response. Returns None on failure."""
    # Strip markdown code blocks if present
    text = re.sub(r"```(?:json)?", "", raw_text).strip()

    # Extract first {...} block
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        logger.warning("matcher.no_json_in_response", job_id=job_id, raw=raw_text[:200])
        return None

    try:
        data = json.loads(match.group())
        return MatchResponse(**data)
    except (json.JSONDecodeError, ValidationError) as e:
        logger.warning("matcher.parse_error", job_id=job_id, error=str(e), raw=raw_text[:200])
        return None
