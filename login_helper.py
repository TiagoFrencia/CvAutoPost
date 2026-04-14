"""
Login helper — corre en Windows (HOST), NO dentro de Docker.

Abre un browser con interfaz gráfica, esperás a que se detecte el login,
y guarda las cookies en data/cookies/<platform>.json.

Uso:
    python login_helper.py --platform computrabajo
    python login_helper.py --platform indeed
    python login_helper.py --platform zonajobs
    python login_helper.py --platform bumeran
    python login_helper.py --platform workana
    python login_helper.py --platform linkedin     # usa Chrome real (Nodriver)

Dependencias locales (pip install, no en Docker):
    pip install playwright playwright-stealth python-dotenv
    playwright install chromium
"""
import argparse
import asyncio
import os
import sys
from pathlib import Path

# Cargar .env desde la carpeta del proyecto
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

# Necesario antes de importar core/ porque lee settings desde .env
from services.session_manager import SessionManager  # noqa: E402

# ---------------------------------------------------------------------------
# Configuración por plataforma
# ---------------------------------------------------------------------------
PLATFORM_CONFIG = {
    "computrabajo": {
        "login_url":      "https://candidato.ar.computrabajo.com/acceso/",
        "success_url":    "https://candidato.ar.computrabajo.com/**",
        "hint":           "Ingresa tu email, luego la contrasena. Despues del login llegas a candidato.ar.computrabajo.com.",
    },
    "indeed": {
        "login_url":      "https://ar.indeed.com/account/login",
        "success_url":    "https://ar.indeed.com/",
        "hint":           "Iniciá sesión en Indeed. El script detecta cuando te redirige a la home.",
    },
    "zonajobs": {
        "login_url":      "https://www.zonajobs.com.ar/login",
        "success_url":    "**/postulaciones**",
        "hint":           "Iniciá sesión en ZonaJobs. El script detecta cuando llegás a Mis Postulaciones.",
    },
    "bumeran": {
        "login_url":      "https://www.bumeran.com.ar/login",
        "success_url":    "**/postulaciones**",
        "hint":           "Iniciá sesión en Bumeran. El script detecta cuando llegás a Mis Postulaciones.",
    },
    "workana": {
        "login_url":      "https://www.workana.com/login",
        "success_url":    "**/dashboard**",
        "hint":           "Iniciá sesión en Workana. El script detecta cuando llegás al dashboard.",
    },
    "linkedin": {
        "login_url":      "https://www.linkedin.com/login",
        "success_url":    "**/feed**",
        "hint":           "Iniciá sesión en LinkedIn. El script detecta cuando llegás al feed.",
    },
}

CHROME_PATH = os.getenv(
    "CHROME_EXECUTABLE_PATH",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
)


# ---------------------------------------------------------------------------
# Playwright (todas las plataformas excepto LinkedIn)
# ---------------------------------------------------------------------------
def _wait_for_user(platform: str, page) -> None:
    """Show a always-on-top popup. User clicks OK only after completing login."""
    import tkinter as tk
    from tkinter import messagebox

    root = tk.Tk()
    root.withdraw()
    root.lift()
    root.attributes("-topmost", True)
    messagebox.showinfo(
        f"Login Helper — {platform}",
        f"1. Logueate en el browser que se abrio.\n"
        f"2. Cuando estes en el panel principal,\n"
        f"   volvé acá y hace click en OK.",
        parent=root,
    )
    root.destroy()


def login_with_playwright(platform: str) -> None:
    from playwright.sync_api import sync_playwright
    from playwright_stealth import stealth_sync

    cfg = PLATFORM_CONFIG[platform]
    manager = SessionManager()

    print(f"\n>>> Plataforma: {platform}")
    print(f">>> Se va a abrir el browser en: {cfg['login_url']}")
    print(">>> Logueate normalmente (incluyendo 2FA si lo pide).")
    print(">>> Cuando termines, volvé acá y presioná ENTER.\n")

    with sync_playwright() as p:
        # Use a real installed browser to bypass bot detection
        launched = False
        for channel in ("msedge", "chrome"):
            try:
                browser = p.chromium.launch(
                    headless=False,
                    slow_mo=30,
                    channel=channel,
                    args=["--start-maximized"],
                )
                print(f">>> Usando {channel}")
                launched = True
                break
            except Exception:
                continue
        if not launched:
            browser = p.chromium.launch(headless=False, slow_mo=30, args=["--start-maximized"])
        context = browser.new_context(
            no_viewport=True,
            locale="es-AR",
        )
        page = context.new_page()
        stealth_sync(page)
        page.goto(cfg["login_url"])

        _wait_for_user(platform, page)

        print(">>> Guardando cookies...")
        cookies = context.cookies()
        manager.save_cookies(platform, cookies)
        browser.close()

    print(f"\n[OK] {platform}: {len(cookies)} cookies guardadas.")
    print("[OK] Podes cerrar el browser si quedo abierto.")


# ---------------------------------------------------------------------------
# Nodriver / Chrome real (LinkedIn solamente)
# ---------------------------------------------------------------------------
async def _login_linkedin_async() -> None:
    try:
        import nodriver as uc
    except ImportError:
        print("[!] Instalá nodriver:  pip install nodriver")
        sys.exit(1)

    if not Path(CHROME_PATH).exists():
        print(f"[!] Chrome no encontrado: {CHROME_PATH}")
        print("    Configurá CHROME_EXECUTABLE_PATH en el .env")
        sys.exit(1)

    cfg = PLATFORM_CONFIG["linkedin"]
    manager = SessionManager()

    print(f"\n>>> Plataforma: linkedin (Chrome real)")
    print(f">>> {cfg['hint']}\n")

    browser = await uc.start(
        browser_executable_path=CHROME_PATH,
        headless=False,
    )
    tab = await browser.get(cfg["login_url"])

    print(">>> Esperando que llegues al feed de LinkedIn...\n")
    while True:
        await asyncio.sleep(2)
        current_url = tab.url or ""
        if "/feed" in current_url or "/mynetwork" in current_url:
            break

    print(">>> Feed detectado. Guardando cookies...")
    raw_cookies = await tab.browser.cookies.get_all()
    cookies = [
        {
            "name":     c.name,
            "value":    c.value,
            "domain":   c.domain,
            "path":     getattr(c, "path", "/"),
            "expires":  getattr(c, "expires", -1),
            "httpOnly": getattr(c, "http_only", False),
            "secure":   getattr(c, "secure", False),
        }
        for c in raw_cookies
    ]
    browser.stop()

    manager.save_cookies("linkedin", cookies)
    is_valid, days = manager.check_expiry("linkedin")
    li_at = next((c["value"][:8] + "..." for c in cookies if c["name"] == "li_at"), "NO ENCONTRADA")
    if is_valid:
        print(f"\n[OK] linkedin: {len(cookies)} cookies guardadas. Sesion valida ~{days:.0f} dias.")
        print(f"     li_at: {li_at}")
    else:
        print("\n[WARN] linkedin: cookies guardadas pero pueden ser invalidas.")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Login helper — guarda cookies para el bot")
    parser.add_argument(
        "--platform",
        required=True,
        choices=list(PLATFORM_CONFIG.keys()),
        help="Plataforma a loguear",
    )
    args = parser.parse_args()

    if args.platform == "linkedin":
        asyncio.run(_login_linkedin_async())
    else:
        login_with_playwright(args.platform)


if __name__ == "__main__":
    main()
