from pathlib import Path
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Database
    db_url: str = Field(..., alias="DB_URL")

    # AI — Gemini (primary)
    gemini_api_key: str = Field(..., alias="GEMINI_API_KEY")

    # AI — Ollama fallback (local Gemma 4, used when Gemini quota is exhausted)
    # Ollama runs on the Windows host; the bot accesses it via host.docker.internal from inside Docker.
    ollama_url: str = Field(default="http://host.docker.internal:11434", alias="OLLAMA_URL")
    ollama_model: str = Field(default="gemma4:e2b-it-q4_K_M", alias="OLLAMA_MODEL")

    # Telegram (optional — bot logs to console until configured)
    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = Field(default="", alias="TELEGRAM_CHAT_ID")

    # Chrome executable path for LinkedIn via Nodriver.
    # Defaults to the Linux path when running inside Docker (after installing google-chrome-stable).
    # Override with CHROME_EXECUTABLE_PATH in .env when running on Windows host.
    chrome_executable_path: str = Field(
        default="/usr/bin/google-chrome-stable",
        alias="CHROME_EXECUTABLE_PATH",
    )

    # Daily limits
    max_applications_per_day_linkedin: int = Field(default=5, alias="MAX_APPLICATIONS_PER_DAY_LINKEDIN")
    max_applications_per_day_computrabajo: int = Field(default=15, alias="MAX_APPLICATIONS_PER_DAY_COMPUTRABAJO")
    max_applications_per_day_indeed: int = Field(default=15, alias="MAX_APPLICATIONS_PER_DAY_INDEED")

    # Paths (relative to project root)
    data_dir: Path = Path("data")
    cookies_dir: Path = Path("data/cookies")
    screenshots_dir: Path = Path("data/screenshots")
    cvs_dir: Path = Path("data/cvs")

    # AI scoring thresholds
    score_auto_apply: int = 80
    score_review: int = 60

    # Fernet key for encrypting stored credentials (auto-login).
    # Generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    credentials_secret: str = Field(default="", alias="CREDENTIALS_SECRET")

    # Browser mode (set PLAYWRIGHT_HEADLESS=false for local debugging)
    playwright_headless: bool = Field(default=True, alias="PLAYWRIGHT_HEADLESS")

    # Gmail email monitor (optional — leave blank to disable)
    # Requires IMAP enabled + an App Password (not the regular Gmail password)
    gmail_address: str = Field(default="", alias="GMAIL_ADDRESS")
    gmail_app_password: str = Field(default="", alias="GMAIL_APP_PASSWORD")


settings = Settings()
