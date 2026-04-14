# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**auto_applier_bot** — an autonomous job application bot for Tiago Frencia. It scrapes job listings from 10+ platforms, scores them against a CV using Gemini AI, auto-applies to high-score jobs, and reports results via Telegram. Runs locally via Docker Compose ($0 hosting).

Two CV profiles exist in `data/cvs/`:
- `cv_remoto.json` — tech/programming roles (remote/international) → Perfil A
- `cv_local.json` — non-tech roles in Río Cuarto, Argentina → Perfil B

Pre-defined form answers live in `data/answers.yaml` (zero LLM cost). Only unknown form fields go to the LLM.

## Commands

```bash
# Start all services (bot + dashboard + db)
docker-compose up -d

# Dashboard available at http://localhost:8080

# Run DB migrations
docker-compose exec bot alembic upgrade head

# Seed initial data (platforms + CV profiles)
docker-compose exec bot python main.py seed

# Manual LinkedIn cookie setup (run once, opens real Chrome)
docker-compose exec bot python login_helper.py --platform linkedin

# Validate LinkedIn session cookies
docker-compose exec bot python -c "
from services.session_manager import SessionManager
sm = SessionManager()
is_valid, days = sm.check_expiry('linkedin')
print(f'Valid: {is_valid}, Days remaining: {days}')
"

# Run tests
docker-compose exec bot pytest tests/

# Run a single test file
docker-compose exec bot pytest tests/test_scrapers/test_getonboard.py -v

# View logs
docker-compose logs -f bot
```

## Architecture

```
Scheduler (APScheduler) → Pipeline Orchestrator
                               │
        ┌──────────────────────┼──────────────────────┐
        │                      │                       │
   scrapers/              ai_engine/              services/
   (fetch jobs)       (score + form fill)     (apply + notify)
        │                      │                       │
        └──────────────────────┴───────────────────────┘
                               │
                         PostgreSQL
                    (jobs, applications, reports)
```

### Scoring and Auto-Apply Logic
- Score < 60 → `SKIPPED`
- 60–79 → `REVIEW_SCORE` (manual approval via Telegram)
- ≥ 80 → `AUTO_APPLY` → `APPLIED` / `REVIEW_FORM` / `FAILED` (max 2 retries)

### Key Modules (planned structure)

| Directory | Responsibility |
|---|---|
| `core/` | Config (Pydantic Settings), DB engine, ORM models, enums |
| `scrapers/` | One file per platform; all inherit `BaseScraper.fetch_jobs()` |
| `ai_engine/` | `job_matcher.py` (CV↔job scoring), `form_filler.py` (LLM for unknown fields), `context_cache.py` (Gemini context caching for CV) |
| `services/` | `applier.py` (Playwright submit), `session_manager.py` (cookie inject/persist), `notifier.py` (Telegram), `screenshot.py` (error debug) |
| `orchestrator/` | `pipeline.py` (full flow), `scheduler.py` (APScheduler), `lock_manager.py` (prevents simultaneous Chromium to avoid OOM) |
| `data/` | CVs (JSON + PDF), cookies (gitignored), screenshots |
| `migrations/` | Alembic versions |

### Platform Access Methods
- **API (no auth):** GetOnBoard, RemoteOK → start here to validate architecture
- **Playwright:** Computrabajo, Indeed, ZonaJobs, Bumeran, Workana
- **Nodriver/CDP + cookies:** LinkedIn → uses real Chrome (not Chromium) to pass WAF/TLS fingerprint checks; max 5 applications/day

### LinkedIn Session Management
LinkedIn login is done **once manually** via `login_helper.py`, which saves cookies to `data/cookies/linkedin.json`. The bot injects these cookies on each run. The critical cookie is `li_at`. `SessionManager` alerts via Telegram 48h before expiry. **Never use Chromium as a substitute** — the JA3/JA4 TLS fingerprint difference is why Nodriver bypasses detection.

## Environment Variables (.env)

```env
DB_URL=postgresql://bot_user:bot_password@db:5432/auto_applier
GEMINI_API_KEY=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
CHROME_EXECUTABLE_PATH=C:\Program Files\Google\Chrome\Application\chrome.exe
MAX_APPLICATIONS_PER_DAY_LINKEDIN=5
MAX_APPLICATIONS_PER_DAY_COMPUTRABAJO=15
MAX_APPLICATIONS_PER_DAY_INDEED=15
```

## Important Constraints

- `data/cookies/` must be in `.gitignore` — cookies are encrypted at rest via Fernet in `session_manager.py`
- LinkedIn login must always come from the user's real residential IP, never a proxy
- `lock_manager.py` must prevent concurrent Playwright instances (Chromium ~1.5 GB RAM each)
- `cv_local.json` has `profile_context` explicitly saying: **do not mention programming projects or IT training** when filling forms for local non-tech roles
- `answers.yaml` is checked first in `form_filler.py`; only send to LLM if no key matches
