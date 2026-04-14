"""
Email Monitor — checks Gmail for replies to job applications and forwards
relevant emails to Telegram.

How it works:
  1. Connects to Gmail via IMAP SSL (App Password — no OAuth needed)
  2. Searches the last 30 days for emails from known job-platform domains
     AND for emails whose subject contains job-application keywords
  3. Classifies each new email into one of five categories:
       INTERVIEW  — the company wants to schedule a call/interview
       OFFER      — a formal job offer
       REJECTION  — "we went with other candidates"
       RECEIVED   — application acknowledgement / "we received your CV"
       REPLY      — any other reply from a job platform
  4. Skips emails already processed (persisted in data/email_seen.json)
  5. Sends a formatted Telegram notification per new email

Usage:
  from services.email_monitor import run_email_check
  run_email_check()          # call from scheduler

Requires .env:
  GMAIL_ADDRESS=tiagoofrenciaa@gmail.com
  GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx
"""
import email
import imaplib
import json
import re
from datetime import datetime, timedelta, timezone
from email.header import decode_header
from pathlib import Path
from typing import Optional

import structlog

from core.config import settings
from services import notifier

logger = structlog.get_logger()

SEEN_PATH = Path("data/email_seen.json")
GMAIL_IMAP_HOST = "imap.gmail.com"
GMAIL_IMAP_PORT = 993

# ── Sender domains that belong to job platforms ───────────────────────────────

PLATFORM_DOMAINS = {
    "linkedin.com":       "LinkedIn",
    "linkedin.co.ar":     "LinkedIn",
    "e.linkedin.com":     "LinkedIn",
    "indeed.com":         "Indeed",
    "indeedmail.com":     "Indeed",
    "ar.indeed.com":      "Indeed",
    "computrabajo.com":   "Computrabajo",
    "computrabajo.com.ar":"Computrabajo",
    "zonajobs.com.ar":    "ZonaJobs",
    "bumeran.com.ar":     "Bumeran",
    "remoteok.com":       "RemoteOK",
    "weworkremotely.com": "WeWorkRemotely",
    "workana.com":        "Workana",
    "getonboard.com":     "GetOnBoard",
}

# Subject/body keywords that indicate a job-related email even from unknown senders
JOB_SUBJECT_KEYWORDS = [
    "postulaci", "candidat", "aplicaci", "oferta", "empleo", "vacante",
    "entrevista", "interview", "job offer", "job application", "your application",
    "we reviewed", "revisamos", "proceso de selección", "selection process",
    "citamos", "nos comunicamos", "hiring", "reclutamiento", "recruiter",
    "proceso", "bienvenido al equipo", "welcome to the team",
]

# ── Email classification ──────────────────────────────────────────────────────

# Each category is a tuple (category_name, emoji, keyword_lists).
# Keywords are checked against the subject (case-insensitive).
CATEGORIES = [
    ("INTERVIEW", "🎯", [
        "entrevista", "interview", "llamada", "llamado", "videollamada",
        "video call", "video interview", "agendar", "schedule", "te citamos",
        "nos gustaría conocerte", "queremos conocerte", "meet with",
        "siguiente etapa", "next step", "avanzaste", "pasaste a",
        "avanzas en", "segunda etapa", "primera etapa",
    ]),
    ("OFFER", "🎉", [
        "oferta de trabajo", "job offer", "offer letter", "bienvenido al equipo",
        "welcome to the team", "contrato", "employment offer", "te ofrecemos",
        "felicitaciones", "congratulations", "start date", "fecha de inicio",
    ]),
    ("REJECTION", "❌", [
        "no avanzar", "no avanzarás", "no continúas", "no seleccionado",
        "no fue seleccionado", "other candidates", "otros candidatos",
        "not moving forward", "declined", "not selected", "no seguirás",
        "no pudimos avanzar", "lamentamos", "unfortunately", "lo sentimos",
        "no cumplís", "no cumples", "we won't be moving", "decided to move",
        "position has been filled", "puesto cubierto",
    ]),
    ("RECEIVED", "📨", [
        "recibimos tu", "recibimos su", "received your application",
        "aplicación recibida", "postulación recibida", "hemos recibido",
        "we received", "gracias por postularte", "thank you for applying",
        "gracias por tu interés", "thank you for your interest",
        "vimos tu perfil", "revisaremos", "we will review",
    ]),
]


def classify(subject: str) -> tuple[str, str]:
    """Return (category, emoji) for the given subject line."""
    subj_lower = subject.lower()
    for category, emoji, keywords in CATEGORIES:
        if any(kw in subj_lower for kw in keywords):
            return category, emoji
    return "REPLY", "💬"


# ── Persistent seen-set ───────────────────────────────────────────────────────

def _load_seen() -> set[str]:
    if SEEN_PATH.exists():
        try:
            return set(json.loads(SEEN_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
    return set()


def _save_seen(seen: set[str]) -> None:
    try:
        SEEN_PATH.write_text(
            json.dumps(sorted(seen), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning("email_monitor.seen_save_error", error=str(e))


# ── IMAP helpers ──────────────────────────────────────────────────────────────

def _decode_header_value(raw: str) -> str:
    """Decode a possibly-encoded email header value to plain text."""
    parts = decode_header(raw or "")
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            try:
                decoded.append(part.decode(charset or "utf-8", errors="replace"))
            except (LookupError, TypeError):
                # Unknown codec (e.g. "unknown-8bit") — fall back to UTF-8
                decoded.append(part.decode("utf-8", errors="replace"))
        else:
            decoded.append(str(part))
    return "".join(decoded).strip()


def _extract_sender_domain(from_header: str) -> Optional[str]:
    """Return the domain of the sender, lowercased."""
    match = re.search(r"@([\w.\-]+)", from_header or "")
    return match.group(1).lower() if match else None


def _build_search_criteria(days_back: int = 30) -> str:
    """
    IMAP SEARCH criteria that finds emails either:
      - FROM a known job-platform domain, OR
      - With a job-related keyword in the subject
    Only looks at the last `days_back` days.
    """
    since_date = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%d-%b-%Y")

    # Build OR chain for platform domains
    domain_searches = [f'FROM "@{domain}"' for domain in PLATFORM_DOMAINS]
    # Build OR chain for subject keywords (pick the most discriminating ones to keep it fast)
    subject_searches = [
        'SUBJECT "entrevista"',
        'SUBJECT "interview"',
        'SUBJECT "oferta"',
        'SUBJECT "job offer"',
        'SUBJECT "postulaci"',
        'SUBJECT "aplicaci"',
        'SUBJECT "candidat"',
        'SUBJECT "selección"',
        'SUBJECT "proceso"',
    ]

    all_searches = domain_searches + subject_searches

    # IMAP OR is binary: OR A B. Chain them: OR A (OR B (OR C D))
    def _build_or(items: list[str]) -> str:
        if len(items) == 1:
            return items[0]
        if len(items) == 2:
            return f"OR {items[0]} {items[1]}"
        return f"OR {items[0]} ({_build_or(items[1:])})"

    return f"SINCE {since_date} ({_build_or(all_searches)})"


def _is_job_related(subject: str, sender_domain: Optional[str]) -> bool:
    """
    Additional relevance check after IMAP search.
    Returns True if this email is worth notifying about.
    """
    if sender_domain and any(
        sender_domain == d or sender_domain.endswith("." + d)
        for d in PLATFORM_DOMAINS
    ):
        return True
    subj_lower = (subject or "").lower()
    return any(kw in subj_lower for kw in JOB_SUBJECT_KEYWORDS)


# ── Main check function ───────────────────────────────────────────────────────

def run_email_check() -> int:
    """
    Connect to Gmail, scan for new job-related emails, send Telegram alerts.
    Returns the number of new emails notified.
    Safe to call even if Gmail credentials are not configured (returns 0).
    """
    if not settings.gmail_address or not settings.gmail_app_password:
        logger.debug("email_monitor.disabled_no_credentials")
        return 0

    logger.info("email_monitor.start", account=settings.gmail_address)

    try:
        mail = imaplib.IMAP4_SSL(GMAIL_IMAP_HOST, GMAIL_IMAP_PORT)
        mail.login(settings.gmail_address, settings.gmail_app_password)
    except imaplib.IMAP4.error as e:
        logger.error("email_monitor.login_failed", error=str(e))
        notifier.alert(
            f"Monitor de email: error de autenticación Gmail.\n"
            f"Verificá GMAIL_APP_PASSWORD en .env\n{e}"
        )
        return 0
    except Exception as e:
        logger.error("email_monitor.connection_error", error=str(e))
        return 0

    try:
        mail.select("INBOX")
        criteria = _build_search_criteria(days_back=30)
        _, msg_ids_raw = mail.search(None, criteria)
        msg_ids = (msg_ids_raw[0] or b"").split()

        if not msg_ids:
            logger.info("email_monitor.no_candidates")
            return 0

        logger.info("email_monitor.candidates_found", count=len(msg_ids))
        seen = _load_seen()
        new_count = 0

        for msg_id in msg_ids:
            uid = msg_id.decode()
            if uid in seen:
                continue

            try:
                _, msg_data = mail.fetch(msg_id, "(RFC822)")
                raw = msg_data[0][1]
                msg = email.message_from_bytes(raw)

                subject = _decode_header_value(msg.get("Subject", "(sin asunto)"))
                from_header = _decode_header_value(msg.get("From", ""))
                date_str = msg.get("Date", "")
                sender_domain = _extract_sender_domain(from_header)

                if not _is_job_related(subject, sender_domain):
                    seen.add(uid)
                    continue

                platform_name = next(
                    (name for domain, name in PLATFORM_DOMAINS.items()
                     if sender_domain and (sender_domain == domain or sender_domain.endswith("." + domain))),
                    None,
                )

                category, emoji = classify(subject)
                _notify_email(emoji, category, subject, from_header, platform_name, date_str)
                seen.add(uid)
                new_count += 1

            except Exception as e:
                logger.warning("email_monitor.msg_error", msg_id=uid, error=str(e))
                seen.add(uid)  # skip on error to avoid re-processing

        _save_seen(seen)
        logger.info("email_monitor.done", new=new_count, total_scanned=len(msg_ids))
        return new_count

    finally:
        try:
            mail.logout()
        except Exception:
            pass


def _notify_email(
    emoji: str,
    category: str,
    subject: str,
    from_header: str,
    platform: Optional[str],
    date_str: str,
) -> None:
    CATEGORY_LABELS = {
        "INTERVIEW": "¡Entrevista!",
        "OFFER":     "¡Oferta de trabajo!",
        "REJECTION": "No avanzaste",
        "RECEIVED":  "Postulación recibida",
        "REPLY":     "Respuesta recibida",
    }
    label = CATEGORY_LABELS.get(category, "Email de trabajo")
    platform_line = f"\n🏢 Plataforma: <b>{platform}</b>" if platform else ""

    text = (
        f"{emoji} <b>{label}</b>"
        f"{platform_line}\n"
        f"📧 De: {_escape_html(from_header)}\n"
        f"📌 Asunto: <b>{_escape_html(subject)}</b>\n"
        f"📅 {date_str}"
    )
    notifier.send_message(text)
    logger.info(
        "email_monitor.notification_sent",
        category=category,
        platform=platform,
        subject=subject[:80],
    )


def _escape_html(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
