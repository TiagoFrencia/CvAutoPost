"""
Tests for the email monitor.
All IMAP and Telegram calls are mocked.
"""
import json
import pytest
from unittest.mock import MagicMock, patch, call

from services.email_monitor import (
    classify,
    _decode_header_value,
    _extract_sender_domain,
    _is_job_related,
    _load_seen,
    _save_seen,
    run_email_check,
)


# ── classify() ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("subject,expected", [
    ("Te invitamos a una entrevista para el puesto", "INTERVIEW"),
    ("Interview invitation — Full Stack Developer", "INTERVIEW"),
    ("Nos gustaría conocerte — Acme Corp", "INTERVIEW"),
    ("Avanzaste a la siguiente etapa", "INTERVIEW"),
    ("Oferta de trabajo — Bienvenido al equipo", "OFFER"),
    ("Job offer from TechCorp", "OFFER"),
    ("Felicitaciones, te seleccionamos", "OFFER"),
    ("Lamentamos informarte que no avanzarás", "REJECTION"),
    ("Unfortunately we have decided to move forward with other candidates", "REJECTION"),
    ("No pudimos avanzar con tu candidatura", "REJECTION"),
    ("Recibimos tu postulación — Full Stack", "RECEIVED"),
    ("Thank you for applying to our position", "RECEIVED"),
    ("Gracias por tu interés en la oferta", "RECEIVED"),
    ("Mensaje de LinkedIn sobre tu perfil", "REPLY"),
    ("Actualizaciones de tu cuenta", "REPLY"),
])
def test_classify(subject, expected):
    category, _ = classify(subject)
    assert category == expected


# ── _decode_header_value() ────────────────────────────────────────────────────

def test_decode_header_plain():
    assert _decode_header_value("Hello World") == "Hello World"


def test_decode_header_encoded():
    # UTF-8 encoded header
    encoded = "=?utf-8?b?SGVsbG8gV29ybGQ=?="  # "Hello World" in base64
    assert _decode_header_value(encoded) == "Hello World"


def test_decode_header_empty():
    assert _decode_header_value("") == ""


# ── _extract_sender_domain() ──────────────────────────────────────────────────

@pytest.mark.parametrize("from_header,expected_domain", [
    ("LinkedIn <jobs@linkedin.com>", "linkedin.com"),
    ("Indeed <noreply@indeedmail.com>", "indeedmail.com"),
    ("Empresa <rrhh@acmecorp.com.ar>", "acmecorp.com.ar"),
    ("no email here", None),
    ("", None),
])
def test_extract_sender_domain(from_header, expected_domain):
    assert _extract_sender_domain(from_header) == expected_domain


# ── _is_job_related() ─────────────────────────────────────────────────────────

def test_is_job_related_by_domain():
    assert _is_job_related("Actualización de cuenta", "linkedin.com") is True
    assert _is_job_related("Anything", "computrabajo.com.ar") is True


def test_is_job_related_by_subject_keyword():
    assert _is_job_related("Invitación a entrevista técnica", "randomdomain.com") is True
    assert _is_job_related("Tu aplicación fue recibida", None) is True


def test_is_job_related_false():
    # Subjects with no job keywords AND sender domains not in PLATFORM_DOMAINS
    assert _is_job_related("Tu factura de Netflix", "netflix.com") is False
    assert _is_job_related("Newsletter de tecnología", "example.com") is False
    assert _is_job_related("Confirmación de compra", "tienda.com") is False


# ── _load_seen / _save_seen ───────────────────────────────────────────────────

def test_seen_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr("services.email_monitor.SEEN_PATH", tmp_path / "seen.json")
    seen = {"1", "2", "3"}
    _save_seen(seen)
    loaded = _load_seen()
    assert loaded == seen


def test_load_seen_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr("services.email_monitor.SEEN_PATH", tmp_path / "nonexistent.json")
    assert _load_seen() == set()


# ── run_email_check() — disabled when no credentials ─────────────────────────

def test_run_email_check_disabled_without_credentials(monkeypatch):
    monkeypatch.setattr("services.email_monitor.settings",
                        MagicMock(gmail_address="", gmail_app_password=""))
    result = run_email_check()
    assert result == 0


# ── run_email_check() — full flow with mocked IMAP ───────────────────────────

def _make_raw_email(subject: str, from_addr: str) -> bytes:
    """Build a minimal RFC822 email as bytes with explicit UTF-8 content type."""
    return (
        f"From: {from_addr}\r\n"
        f"Subject: {subject}\r\n"
        f"Date: Mon, 13 Apr 2026 10:00:00 +0000\r\n"
        f"Content-Type: text/plain; charset=utf-8\r\n"
        f"MIME-Version: 1.0\r\n"
        f"\r\nBody text.\r\n"
    ).encode("utf-8")


def test_run_email_check_new_interview(tmp_path, monkeypatch):
    monkeypatch.setattr("services.email_monitor.SEEN_PATH", tmp_path / "seen.json")
    monkeypatch.setattr(
        "services.email_monitor.settings",
        MagicMock(gmail_address="test@gmail.com", gmail_app_password="secret"),
    )

    raw = _make_raw_email(
        subject="Te invitamos a una entrevista — Full Stack Developer",
        from_addr="noreply@linkedin.com",
    )

    mock_imap = MagicMock()
    mock_imap.search.return_value = (None, [b"42"])
    mock_imap.fetch.return_value = (None, [(None, raw)])

    with patch("services.email_monitor.imaplib.IMAP4_SSL", return_value=mock_imap), \
         patch("services.email_monitor.notifier.send_message") as mock_notify:
        count = run_email_check()

    assert count == 1
    mock_notify.assert_called_once()
    msg_text = mock_notify.call_args[0][0]
    assert "Entrevista" in msg_text
    assert "LinkedIn" in msg_text

    # Message ID should be persisted
    seen = _load_seen()
    assert "42" in seen


def test_run_email_check_skips_already_seen(tmp_path, monkeypatch):
    monkeypatch.setattr("services.email_monitor.SEEN_PATH", tmp_path / "seen.json")
    monkeypatch.setattr(
        "services.email_monitor.settings",
        MagicMock(gmail_address="test@gmail.com", gmail_app_password="secret"),
    )
    # Pre-populate seen with the message ID
    _save_seen({"99"})

    mock_imap = MagicMock()
    mock_imap.search.return_value = (None, [b"99"])

    with patch("services.email_monitor.imaplib.IMAP4_SSL", return_value=mock_imap), \
         patch("services.email_monitor.notifier.send_message") as mock_notify:
        count = run_email_check()

    assert count == 0
    mock_notify.assert_not_called()
    mock_imap.fetch.assert_not_called()


def test_run_email_check_skips_irrelevant_email(tmp_path, monkeypatch):
    monkeypatch.setattr("services.email_monitor.SEEN_PATH", tmp_path / "seen.json")
    monkeypatch.setattr(
        "services.email_monitor.settings",
        MagicMock(gmail_address="test@gmail.com", gmail_app_password="secret"),
    )

    raw = _make_raw_email(
        subject="Descuentos especiales de verano",
        from_addr="promo@tienda.com",
    )

    mock_imap = MagicMock()
    mock_imap.search.return_value = (None, [b"7"])
    mock_imap.fetch.return_value = (None, [(None, raw)])

    with patch("services.email_monitor.imaplib.IMAP4_SSL", return_value=mock_imap), \
         patch("services.email_monitor.notifier.send_message") as mock_notify:
        count = run_email_check()

    assert count == 0
    mock_notify.assert_not_called()


def test_run_email_check_no_messages(tmp_path, monkeypatch):
    monkeypatch.setattr("services.email_monitor.SEEN_PATH", tmp_path / "seen.json")
    monkeypatch.setattr(
        "services.email_monitor.settings",
        MagicMock(gmail_address="test@gmail.com", gmail_app_password="secret"),
    )

    mock_imap = MagicMock()
    mock_imap.search.return_value = (None, [b""])

    with patch("services.email_monitor.imaplib.IMAP4_SSL", return_value=mock_imap), \
         patch("services.email_monitor.notifier.send_message") as mock_notify:
        count = run_email_check()

    assert count == 0
    mock_notify.assert_not_called()


def test_run_email_check_login_failure_sends_alert(tmp_path, monkeypatch):
    monkeypatch.setattr("services.email_monitor.SEEN_PATH", tmp_path / "seen.json")
    monkeypatch.setattr(
        "services.email_monitor.settings",
        MagicMock(gmail_address="test@gmail.com", gmail_app_password="wrong"),
    )

    import imaplib as _imap
    mock_imap = MagicMock()
    mock_imap.login.side_effect = _imap.IMAP4.error("Invalid credentials")

    with patch("services.email_monitor.imaplib.IMAP4_SSL", return_value=mock_imap), \
         patch("services.email_monitor.notifier.alert") as mock_alert:
        count = run_email_check()

    assert count == 0
    mock_alert.assert_called_once()


def test_run_email_check_multiple_emails(tmp_path, monkeypatch):
    """Multiple new emails → one Telegram notification each."""
    monkeypatch.setattr("services.email_monitor.SEEN_PATH", tmp_path / "seen.json")
    monkeypatch.setattr(
        "services.email_monitor.settings",
        MagicMock(gmail_address="test@gmail.com", gmail_app_password="secret"),
    )

    emails = {
        b"1": _make_raw_email("Entrevista técnica — Backend Developer", "hr@acme.com"),
        b"2": _make_raw_email("Lamentamos que no avanzarás en el proceso", "noreply@indeed.com"),
        b"3": _make_raw_email("Recibimos tu postulación — React Developer", "jobs@zonajobs.com.ar"),
    }

    mock_imap = MagicMock()
    mock_imap.search.return_value = (None, [b"1 2 3"])
    mock_imap.fetch.side_effect = lambda mid, _: (None, [(None, emails[mid])])

    with patch("services.email_monitor.imaplib.IMAP4_SSL", return_value=mock_imap), \
         patch("services.email_monitor.notifier.send_message") as mock_notify:
        count = run_email_check()

    assert count == 3
    assert mock_notify.call_count == 3
