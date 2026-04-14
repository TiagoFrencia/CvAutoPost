"""
Manages cookie-based sessions for platforms that require login (LinkedIn, etc.).
Cookies are stored as JSON in data/cookies/{platform}.json.
Validates expiry at startup — never makes a live request just to find out cookies expired.

Cookie lifecycle:
  1. Manual login via login_helper.py saves initial cookies.
  2. After each successful Playwright session, save_refreshed_cookies() updates
     them with any new cookies the server issued (natural keep-alive).
  3. check_expiry() only considers authentication cookies — ephemeral infrastructure
     cookies (Cloudflare __cf_bm, Hotjar _hjSession, analytics _ga_*, fingerprints)
     are filtered out to avoid false-expired alarms.
  4. If cookies truly expire and credentials are stored, auto_login() re-authenticates
     automatically (all platforms except LinkedIn).
"""
import base64
import json
import time
from pathlib import Path
from typing import Optional

import structlog

from core.config import settings
from services import notifier

logger = structlog.get_logger()

# ── Transient cookie filter ───────────────────────────────────────────────────
# These cookies must NOT trigger expiry alarms — they are short-lived
# infrastructure/analytics cookies that are refreshed automatically on every
# page request and have nothing to do with whether the user is authenticated.

TRANSIENT_COOKIE_NAMES: frozenset[str] = frozenset({
    "__cf_bm",                  # Cloudflare Bot Management — TTL 30 min, refreshed per-request
    "__cfruid",                 # Cloudflare session affinity
    "lout",                     # Logout invalidation token (Computrabajo)
    "frpo-cki",                 # Navent fingerprint (ZonaJobs/Bumeran) — short-lived
    "frpo-cki-lax",             # Navent fingerprint variant
    "XSRF-TOKEN",               # CSRF token — usually session-scoped
    "csrf_token",
    "registro_actividad",       # Navent activity tracker (ZonaJobs/Bumeran) — not auth
    "lidc",                     # LinkedIn data-center routing — expires daily, not auth
    "_guid",                    # LinkedIn browser/device identifier — short-lived, not auth
    "lms_ads",                  # LinkedIn ads measurement — not auth
    "lms_analytics",            # LinkedIn analytics — not auth
    "AnalyticsSyncHistory",     # LinkedIn analytics sync — not auth
    "aam_uuid",                 # Adobe Audience Manager UUID — analytics, not auth
    "_gcl_au",                  # Google Click ID (advertising) — not auth
    "UserMatchHistory",         # LinkedIn ad targeting — not auth
    "timezone",                 # LinkedIn timezone preference — not auth
    "_pxvid",                   # PerimeterX visitor fingerprint — not auth
    "_uetvid",                  # Microsoft/Bing UET visitor ID — ads tracking, not auth
    "dfpfpt",                   # DoubleClick first-party tracking — not auth
    "sdui_ver",                 # LinkedIn UI version indicator — not auth
    "li_sugr",                  # LinkedIn suggestion/personalization — not auth
    "li_theme",                 # LinkedIn UI theme preference — not auth
    "li_theme_set",             # LinkedIn UI theme preference — not auth
    "visit",                    # LinkedIn visit counter — not auth
    "g_state",                  # Google OAuth state — not a session cookie
    "fptctx2",                  # First-party tracking context — not auth
    "FPLC",                     # Google Ads first-party (Indeed) — not auth
    "appcookie[activeSession]", # Workana app state indicator — real auth is workana_session
    "ANONCHK",                  # Microsoft tracking (Computrabajo/Navent) — TTL ~10 min, not auth
    "_clsk",                    # Microsoft Clarity session — not auth
    "test_cookie",              # Google/generic test cookie — TTL ~10 min, not auth
    "__sts",                    # Navent session tracker — short-lived, not auth
    "__stgeo",                  # Navent geo tracker — not auth
    "__stbpnenable",            # Navent tracker — not auth
})

TRANSIENT_COOKIE_PREFIXES: tuple[str, ...] = (
    "_ga",             # Google Analytics (_ga, _ga_XXXX)
    "_gid",            # Google Analytics session
    "_hjSession",      # Hotjar session analytics
    "_hjSessionUser",  # Hotjar user
    "__utm",           # Legacy Google Analytics
    "frpo-",           # Navent fingerprint variants
    "_fbp",            # Facebook pixel
    "_pin_",           # Pinterest
    "_sp_",            # Snowplow analytics (_sp_id.*, _sp_ses.* — Indeed)
    "AMCV_",           # Adobe Marketing Cloud visitor ID (dynamic name with org ID)
)

# If a cookie already expired AND expired less than this many seconds ago,
# it is very likely a transient cookie (auth cookies don't expire in minutes).
_SHORT_TTL_WINDOW_SECS = 7_200  # 2 hours


class SessionManager:
    def __init__(self):
        self.cookies_dir = settings.cookies_dir
        self.cookies_dir.mkdir(parents=True, exist_ok=True)

    def cookies_path(self, platform: str) -> Path:
        return self.cookies_dir / f"{platform}.json"

    def credentials_path(self, platform: str) -> Path:
        return self.cookies_dir / f"{platform}.credentials"

    def has_cookies(self, platform: str) -> bool:
        return self.cookies_path(platform).exists()

    def load_cookies(self, platform: str) -> list[dict]:
        path = self.cookies_path(platform)
        if not path.exists():
            raise FileNotFoundError(
                f"No cookies for '{platform}'. Run: python login_helper.py --platform {platform}"
            )
        return json.loads(path.read_text(encoding="utf-8"))

    def save_cookies(self, platform: str, cookies: list[dict]) -> None:
        path = self.cookies_path(platform)
        path.write_text(json.dumps(cookies, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info("session.cookies_saved", platform=platform, path=str(path))

    # ── Expiry check (filters ephemeral cookies) ──────────────────────────────

    def check_expiry(self, platform: str) -> tuple[bool, float]:
        """
        Check cookie expiry without opening any browser.
        Returns (is_valid, days_remaining).

        Only considers authentication cookies. Ephemeral cookies
        (Cloudflare, Hotjar, analytics, fingerprints) are excluded so they
        cannot trigger false-expired alarms — they have nothing to do with
        whether the user is authenticated.
        """
        if not self.has_cookies(platform):
            logger.warning("session.no_cookies", platform=platform)
            return False, 0.0

        cookies = self.load_cookies(platform)
        now = time.time()
        auth_expires: list[float] = []

        for c in cookies:
            name = c.get("name", "")
            expires = c.get("expires")

            # Skip cookies without an explicit expiry field (session cookies
            # with no expiry live until the browser closes — they don't expire)
            if not isinstance(expires, (int, float)) or expires <= 0:
                continue

            # Skip by exact name
            if name in TRANSIENT_COOKIE_NAMES:
                logger.debug("session.skip_transient_cookie", platform=platform, name=name)
                continue

            # Skip by prefix
            if name.startswith(TRANSIENT_COOKIE_PREFIXES):
                logger.debug("session.skip_analytics_cookie", platform=platform, name=name)
                continue

            # Skip recently-expired short-TTL cookies: if the cookie expired
            # less than 2 hours ago, its entire lifetime was very short —
            # auth cookies don't expire in minutes.
            if expires < now and (now - expires) < _SHORT_TTL_WINDOW_SECS:
                logger.debug(
                    "session.skip_short_ttl_cookie",
                    platform=platform, name=name,
                    expired_mins_ago=int((now - expires) / 60),
                )
                continue

            auth_expires.append(expires)

        if not auth_expires:
            # No auth cookies with explicit expiry after filtering.
            # Session cookies (no expiry field) are valid indefinitely.
            return True, 999.0

        min_expires = min(auth_expires)
        days_remaining = (min_expires - now) / 86400

        if days_remaining < 0:
            logger.warning("session.cookies_expired", platform=platform, days=days_remaining)
            notifier.alert(
                f"Cookies de {platform} han EXPIRADO. "
                f"Ejecutá: python login_helper.py --platform {platform}"
            )
            return False, days_remaining

        if days_remaining < 7:
            logger.warning("session.cookies_expiring_soon", platform=platform, days=days_remaining)
            notifier.alert(
                f"Cookies de {platform} expiran en {days_remaining:.1f} días. "
                f"Ejecutá: python login_helper.py --platform {platform}"
            )

        return True, days_remaining

    # ── Cookie keep-alive (save refreshed cookies after sessions) ─────────────

    def save_refreshed_cookies(self, context, platform: str) -> None:
        """
        After a successful Playwright session, persist the updated cookies back
        to disk. The server may have issued new cookies or extended expiry on
        existing ones — saving them keeps the session alive longer.

        Call this ONLY on the success path (not on CAPTCHA/auth-error paths).
        """
        try:
            fresh = context.cookies()
            if not fresh:
                logger.debug("session.no_cookies_to_refresh", platform=platform)
                return
            self.save_cookies(platform, fresh)
            logger.info("session.cookies_refreshed", platform=platform, count=len(fresh))
        except Exception as e:
            # Non-fatal — log and continue. The old cookies remain on disk.
            logger.warning("session.refresh_failed", platform=platform, error=str(e))

    async def save_refreshed_cookies_nodriver(self, tab, platform: str) -> None:
        """
        Extract cookies from a nodriver tab via CDP and persist to disk.
        Used by the LinkedIn applier (which uses nodriver instead of Playwright).
        """
        try:
            raw_cookies = await tab.browser.cookies.get_all()
            if not raw_cookies:
                return
            serialized = [
                {
                    "name":     c.name,
                    "value":    c.value,
                    "domain":   c.domain,
                    "path":     getattr(c, "path", "/"),
                    "expires":  getattr(c, "expires", -1),
                    "httpOnly": getattr(c, "http_only", False),
                    "secure":   getattr(c, "secure", False),
                    "sameSite": getattr(c, "same_site", "Lax"),
                }
                for c in raw_cookies
            ]
            self.save_cookies(platform, serialized)
            logger.info("session.cookies_refreshed_nodriver", platform=platform, count=len(serialized))
        except Exception as e:
            logger.warning("session.refresh_nodriver_failed", platform=platform, error=str(e))

    # ── Encrypted credential storage (for auto-login) ─────────────────────────

    def _get_fernet(self):
        """Derive a Fernet cipher from CREDENTIALS_SECRET. Returns None if not configured."""
        secret = getattr(settings, "credentials_secret", "")
        if not secret:
            return None
        try:
            from cryptography.fernet import Fernet
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
            kdf = PBKDF2HMAC(
                algorithm=hashes.SHA256(),
                length=32,
                salt=b"botcv-salt-v1",
                iterations=100_000,
            )
            key = base64.urlsafe_b64encode(kdf.derive(secret.encode()))
            return Fernet(key)
        except Exception as e:
            logger.warning("session.fernet_init_failed", error=str(e))
            return None

    def save_credentials(self, platform: str, username: str, password: str) -> None:
        """Encrypt and persist platform credentials to disk."""
        f = self._get_fernet()
        if not f:
            raise RuntimeError(
                "CREDENTIALS_SECRET not set in .env — cannot save credentials securely.\n"
                "Generate one with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
            )
        payload = json.dumps({"username": username, "password": password})
        encrypted = f.encrypt(payload.encode())
        self.credentials_path(platform).write_bytes(encrypted)
        logger.info("session.credentials_saved", platform=platform)

    def load_credentials(self, platform: str) -> Optional[tuple[str, str]]:
        """Load and decrypt credentials. Returns (username, password) or None."""
        path = self.credentials_path(platform)
        if not path.exists():
            return None
        f = self._get_fernet()
        if not f:
            return None
        try:
            from cryptography.fernet import InvalidToken
            decrypted = f.decrypt(path.read_bytes())
            data = json.loads(decrypted)
            return data["username"], data["password"]
        except Exception as e:
            logger.error("session.credentials_load_failed", platform=platform, error=str(e))
            return None

    def auto_login(self, platform: str) -> bool:
        """
        Attempt automatic login using stored credentials.
        Returns True if login succeeded and new cookies were saved.
        LinkedIn is explicitly excluded — too risky with anti-bot detection.
        """
        if platform == "linkedin":
            logger.info("session.auto_login_skipped_linkedin")
            return False

        creds = self.load_credentials(platform)
        if not creds:
            logger.info("session.auto_login_no_credentials", platform=platform)
            return False

        username, password = creds
        try:
            from services.auto_login import login_headless
            cookies = login_headless(platform, username, password)
            if cookies:
                self.save_cookies(platform, cookies)
                logger.info("session.auto_login_success", platform=platform)
                return True
            logger.warning("session.auto_login_no_cookies", platform=platform)
            return False
        except Exception as e:
            logger.error("session.auto_login_failed", platform=platform, error=str(e))
            return False

    # ── Playwright injection ───────────────────────────────────────────────────

    def inject_into_playwright(self, context, platform: str) -> None:
        """Inject saved cookies into an existing Playwright browser context."""
        cookies = self.load_cookies(platform)
        normalized_cookies = []
        for cookie in cookies:
            normalized = dict(cookie)
            same_site = normalized.get("sameSite")
            if same_site not in {"Strict", "Lax", "None"}:
                normalized.pop("sameSite", None)
            normalized_cookies.append(normalized)
        context.add_cookies(normalized_cookies)
        logger.info("session.cookies_injected", platform=platform, count=len(normalized_cookies))
