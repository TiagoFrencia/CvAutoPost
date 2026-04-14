"""
Telegram bot handler for REVIEW_SCORE jobs.

When the AI gives a job score of 60-79, it lands in REVIEW_SCORE status
and no application is created. This module:

  1. notify_review_score(job, match_result) — sends a Telegram message with
     Aprobar / Rechazar buttons via InlineKeyboard.

  2. start_bot_polling() — starts a blocking python-telegram-bot
     Application that processes button callbacks:
       - Aprobar → job.status = AUTO_APPLY, create Application QUEUED
       - Rechazar → job.status = SKIPPED

Usage from the scheduler:
  - polling runs in a background thread alongside APScheduler
  - start_polling_thread() spawns it as a daemon thread

Requires: python-telegram-bot[asyncio] (already in requirements.txt as python-telegram-bot)
"""
import asyncio
import threading
from typing import Optional

import structlog

from core.config import settings
from core.database import SessionLocal
from core.enums import ApplicationStatus, JobStatus
from core.models import Application as ApplicationModel, CVProfile, Job, MatchResult

logger = structlog.get_logger()

# Callback data format: "approve:<job_id>" or "reject:<job_id>"
_ACTION_APPROVE = "approve"
_ACTION_REJECT = "reject"


# ── Public API ────────────────────────────────────────────────────────────────

def notify_review_score(job: Job, match_result: MatchResult, cv_profile_name: str) -> None:
    """
    Send a Telegram message with Approve/Reject buttons for a REVIEW_SCORE job.
    Safe to call from sync code — handles event loop detection internally.
    """
    if not _telegram_enabled():
        logger.info("telegram_bot.disabled_skipping_review_notify", job_id=job.id)
        return

    from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup  # lazy — not installed locally

    text = _build_review_message(job, match_result)
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Aprobar", callback_data=f"{_ACTION_APPROVE}:{job.id}:{cv_profile_name}"),
            InlineKeyboardButton("❌ Rechazar", callback_data=f"{_ACTION_REJECT}:{job.id}"),
        ]
    ])

    async def _send():
        bot = Bot(token=settings.telegram_bot_token)
        await bot.send_message(
            chat_id=settings.telegram_chat_id,
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )

    try:
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_send())
        except RuntimeError:
            asyncio.run(_send())
    except Exception as e:
        logger.error("telegram_bot.notify_error", job_id=job.id, error=str(e))


def start_polling_thread() -> Optional[threading.Thread]:
    """
    Start the Telegram bot callback handler in a daemon thread.
    Call this once from cmd_schedule() or cmd_run() before the main loop.
    Returns None if Telegram is not configured.
    """
    if not _telegram_enabled():
        logger.info("telegram_bot.disabled_polling_skipped")
        return None

    t = threading.Thread(target=_run_polling_blocking, name="telegram-bot", daemon=True)
    t.start()
    logger.info("telegram_bot.polling_thread_started")
    return t


# ── Internal ──────────────────────────────────────────────────────────────────

def _telegram_enabled() -> bool:
    return bool(settings.telegram_bot_token and settings.telegram_chat_id)


def _build_review_message(job: Job, match_result: MatchResult) -> str:
    score = match_result.score if match_result else "?"
    reason = (match_result.match_reason or "Sin justificación")[:400] if match_result else ""
    missing = ", ".join(match_result.missing_skills or []) if match_result else ""
    flags = ", ".join(match_result.risk_flags or []) if match_result else ""

    lines = [
        f"🔍 <b>Revisión requerida — Score {score}/100</b>",
        "",
        f"<b>{job.title}</b>",
        f"🏢 {job.company or '?'}",
        f"📍 {job.location or '?'}",
        f"🔗 <a href=\"{job.url}\">Ver oferta</a>",
        "",
        f"<b>Motivo del score:</b> {reason}",
    ]
    if missing:
        lines.append(f"<b>Skills faltantes:</b> {missing}")
    if flags:
        lines.append(f"<b>Alertas:</b> {flags}")
    lines += ["", "¿Querés postularte a esta oferta?"]
    return "\n".join(lines)


def _run_polling_blocking() -> None:
    """Blocking function that runs the bot — meant to run in a thread."""
    asyncio.run(_async_polling())


async def _async_polling() -> None:
    from telegram.ext import Application, CallbackQueryHandler, CommandHandler  # lazy import

    app = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .build()
    )
    app.add_handler(CommandHandler("pendientes", _cmd_pendientes))
    app.add_handler(CallbackQueryHandler(_handle_callback))

    logger.info("telegram_bot.polling_start")
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    # Keep running forever (daemon thread exits when main process does)
    await asyncio.Event().wait()


async def _cmd_pendientes(update, context) -> None:
    """
    /pendientes — list all jobs in REVIEW_SCORE state and resend their buttons.
    Useful if bot was restarted and old messages lost their buttons.
    """
    db = SessionLocal()
    try:
        jobs = db.query(Job).filter(Job.status == JobStatus.REVIEW_SCORE.value).all()
        if not jobs:
            await update.message.reply_text("No hay ofertas pendientes de revisión.")
            return

        await update.message.reply_text(f"Hay {len(jobs)} oferta(s) pendiente(s):")
        for job in jobs:
            match_result = (
                db.query(MatchResult)
                .filter_by(job_id=job.id)
                .order_by(MatchResult.evaluated_at.desc())
                .first()
            )
            # Determine cv_profile_name from match_result

            cv_profile = db.get(CVProfile, match_result.cv_profile_id) if match_result else None
            cv_profile_name = cv_profile.name if cv_profile else "remoto"

            notify_review_score(job, match_result, cv_profile_name)
    finally:
        db.close()


async def _handle_callback(update, context) -> None:
    """Handle Approve / Reject button presses."""
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    parts = data.split(":")

    if len(parts) < 2:
        logger.warning("telegram_bot.invalid_callback", data=data)
        return

    action = parts[0]
    try:
        job_id = int(parts[1])
    except (ValueError, IndexError):
        logger.warning("telegram_bot.invalid_job_id", data=data)
        return

    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        if not job:
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text(f"⚠️ Job #{job_id} no encontrado en la DB.")
            return

        if job.status != JobStatus.REVIEW_SCORE.value:
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text(
                f"ℹ️ <b>{job.title}</b> ya fue procesado (estado: {job.status}).",
                parse_mode="HTML",
            )
            return

        if action == _ACTION_APPROVE:
            cv_profile_name = parts[2] if len(parts) > 2 else "remoto"
            _approve_job(db, job, cv_profile_name)
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text(
                f"✅ <b>{job.title}</b> — aprobada. Se agregó a la cola de postulaciones.",
                parse_mode="HTML",
            )
            logger.info("telegram_bot.approved", job_id=job_id, title=job.title)

        elif action == _ACTION_REJECT:
            job.status = JobStatus.SKIPPED.value
            db.commit()
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text(
                f"❌ <b>{job.title}</b> — rechazada.",
                parse_mode="HTML",
            )
            logger.info("telegram_bot.rejected", job_id=job_id, title=job.title)

        else:
            logger.warning("telegram_bot.unknown_action", action=action)

    finally:
        db.close()


def _approve_job(db, job: Job, cv_profile_name: str) -> None:
    """Move job to AUTO_APPLY and create a QUEUED Application."""
    from core.models import CVProfile

    job.status = JobStatus.AUTO_APPLY.value

    # Find CV profile
    cv_profile = db.query(CVProfile).filter_by(name=cv_profile_name).first()
    if not cv_profile:
        logger.error("telegram_bot.cv_profile_not_found", cv_profile_name=cv_profile_name)
        db.commit()
        return

    # Get score from latest match result
    match_result = (
        db.query(MatchResult)
        .filter_by(job_id=job.id, cv_profile_id=cv_profile.id)
        .order_by(MatchResult.evaluated_at.desc())
        .first()
    )
    score = match_result.score if match_result else 70  # fallback mid-range score

    # Create Application if not already queued
    existing = db.query(ApplicationModel).filter_by(
        job_id=job.id, cv_profile_id=cv_profile.id
    ).first()
    if not existing:
        application = ApplicationModel(
            job_id=job.id,
            cv_profile_id=cv_profile.id,
            status=ApplicationStatus.QUEUED.value,
            priority_score=score,
            retry_count=0,
        )
        db.add(application)

    db.commit()
