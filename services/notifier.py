"""
Telegram notifier.
Falls back to console logging if no token is configured.
"""
import asyncio
import structlog
from core.config import settings

logger = structlog.get_logger()


def _telegram_enabled() -> bool:
    return bool(settings.telegram_bot_token and settings.telegram_chat_id)


def send_message(text: str) -> None:
    if _telegram_enabled():
        _send_telegram(text)
    else:
        logger.info("notifier.console", message=text)


def alert(text: str) -> None:
    """High-priority alert (cookie expiry, CAPTCHA, fatal errors)."""
    send_message(f"⚠️ ALERTA: {text}")


def heartbeat() -> None:
    """Dead man's switch — sent at 07:55 before each pipeline run."""
    send_message("🟢 Sistema online. Iniciando pipeline en 5 min.")


def daily_report(report: dict) -> None:
    from datetime import date
    lines = [
        f"🤖 <b>Reporte Diario — {date.today().strftime('%Y-%m-%d')}</b>",
        "",
        "📊 <b>Resumen:</b>",
        f"  • Ofertas scrapeadas: {report.get('jobs_scraped', 0)}",
        f"  • Con match IA: {report.get('jobs_matched', 0)}",
        f"  • Postulaciones enviadas: {report.get('applied', 0)}",
        f"  • Fallidas: {report.get('failed', 0)}",
        f"  • Para revisión manual: {report.get('review_form', 0)}",
        f"  • Costo API estimado: ${report.get('api_cost_usd', 0):.4f}",
    ]
    breakdown = report.get("platform_breakdown") or {}
    if breakdown:
        lines.append("")
        lines.append("🏷️ <b>Por plataforma:</b>")
        for platform, statuses in sorted(breakdown.items()):
            applied = statuses.get("APPLIED", 0)
            failed = statuses.get("FAILED", 0)
            queued = statuses.get("QUEUED", 0)
            lines.append(f"  • {platform}: {applied} aplic, {failed} fallidas, {queued} en cola")
    send_message("\n".join(lines))


def _send_telegram(text: str) -> None:
    async def _send():
        from telegram import Bot
        bot = Bot(token=settings.telegram_bot_token)
        await bot.send_message(
            chat_id=settings.telegram_chat_id,
            text=text,
            parse_mode="HTML",
        )

    try:
        try:
            loop = asyncio.get_running_loop()
            # Already inside an event loop (e.g., LinkedIn async applier) — schedule as task
            loop.create_task(_send())
        except RuntimeError:
            # No running event loop — safe to create one
            asyncio.run(_send())
    except Exception as e:
        logger.error("notifier.telegram_error", error=str(e))
