# Auto Applier Bot 🤖

> **An autonomous job application bot** — it scrapes 7+ job platforms, scores each listing against my CV using local AI, auto-applies to high-scoring jobs via Playwright, and keeps me updated through Telegram and a live web dashboard. Zero hosting cost.

![Python](https://img.shields.io/badge/Python-3.12-3776ab?logo=python&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-Compose-2496ed?logo=docker&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-336791?logo=postgresql&logoColor=white)
![CI](https://github.com/TiagoFrencia/CvAutoPost/actions/workflows/ci.yml/badge.svg)
![Tests](https://img.shields.io/badge/Tests-115%20passing-22c55e?logo=pytest&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-64748b)

---

## What it does

Every day at **08:00 and 20:00 (Argentina time)**, the bot runs a full pipeline:

1. **Scrapes** job listings from 7 platforms (Computrabajo, Indeed, ZonaJobs, Bumeran, LinkedIn, RemoteOK, WeWorkRemotely)
2. **Scores** each listing against my CV using a local LLM (Ollama/Gemma), with Google Gemini as fallback
3. **Applies** automatically to every job that scores ≥ 80/100 — filling multi-step forms, selecting dropdowns, answering HR questions — without any human input
4. **Sends me a Telegram message** for jobs scoring 60–79 (my approval with one button) and a daily summary report
5. **Checks Gmail** every 2 hours for replies (interview invitations, rejections, offers) and forwards them to Telegram
6. **Shows a live dashboard** at `localhost:8080` with stats, charts and application history

---

## Architecture

```
┌─────────────────────────────────────────────────┐
│          APScheduler  (08:00 / 20:00)            │
│          Email Monitor (every 2 hours)           │
└────────────────────┬────────────────────────────┘
                     │
          ┌──────────▼──────────┐
          │  Pipeline           │
          │  Orchestrator       │
          └──┬──────────┬───────┘
             │          │
   ┌─────────▼───┐  ┌───▼────────────────────┐
   │  Scrapers   │  │     AI Engine           │
   │             │  │  • Job Matcher          │
   │ Computrabajo│  │    (Ollama → Gemini)    │
   │ Indeed      │  │  • Form Filler          │
   │ ZonaJobs    │  │    YAML cache → LLM     │
   │ Bumeran     │  └───┬────────────────────┘
   │ LinkedIn    │      │
   │ RemoteOK    │  ┌───▼────────────────────┐
   │ WWRemotely  │  │     Appliers            │
   └──────┬──────┘  │  • Playwright + stealth │
          │         │  • Nodriver (LinkedIn)  │
          │         │  • Circuit breaker      │
          └────┬────┘  • Cookie management   │
               │        └────────────────────┘
               │
   ┌───────────▼──────────────────────┐
   │          PostgreSQL               │
   │  jobs · applications · reports    │
   └───────────┬──────────────────────┘
               │
   ┌───────────▼──────────────────────┐
   │     Notification Layer           │
   │  Telegram Bot · Email Monitor    │
   │  Web Dashboard (FastAPI)         │
   └──────────────────────────────────┘
```

### Scoring logic

| Score | Action |
|-------|--------|
| < 60  | `SKIPPED` — discarded silently |
| 60–79 | `REVIEW_SCORE` — sent to Telegram with Approve / Reject buttons |
| ≥ 80  | `AUTO_APPLY` — bot applies immediately, max 2 retries on failure |

---

## Tech stack

| Layer | Technology |
|-------|-----------|
| Language | Python 3.12 |
| Web automation | Playwright (Chromium + stealth patch) |
| LinkedIn bypass | Nodriver / CDP with real Chrome (JA3/JA4 fingerprint) |
| AI scoring | Ollama (local Gemma) + Google Gemini fallback |
| AI form filling | Same LLM stack + YAML answer cache (zero-cost for known fields) |
| Database | PostgreSQL 16 via SQLAlchemy 2 + Alembic migrations |
| Scheduling | APScheduler |
| Notifications | python-telegram-bot |
| Email monitoring | IMAP (Gmail App Password) |
| Dashboard | FastAPI + Vanilla JS + Tailwind CSS + Chart.js |
| Containerisation | Docker Compose |
| Testing | pytest (115 tests) |

---

## Platform support

| Platform | Scraper | Applier | Auth |
|----------|---------|---------|------|
| Computrabajo | ✅ | ✅ Playwright + stealth | Cookies |
| Indeed | ✅ | ✅ Playwright multi-step wizard | Cookies |
| ZonaJobs | ✅ | ✅ Playwright | Cookies |
| Bumeran | ✅ | ✅ Playwright | Cookies |
| LinkedIn | ✅ | ✅ Nodriver + real Chrome | Cookies (manual login once) |
| RemoteOK | ✅ | — external links | None |
| WeWorkRemotely | ✅ | — external links | None |
| Workana | — | — | Account unverified |

---

## Quick start

### Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) with WSL2 backend (Windows) or Docker Engine (Linux/macOS)
- Google Chrome installed on the **host** (required for LinkedIn via Nodriver)
- A [Google Gemini API key](https://aistudio.google.com/) (free tier is enough)
- A Telegram bot token ([create one with @BotFather](https://t.me/BotFather)) — optional but recommended
- [Ollama](https://ollama.com/) running on the host with `gemma4:e2b-it-q4_K_M` pulled (optional — Gemini is the fallback)

### 1 · Clone and configure

```bash
git clone https://github.com/TiagoFrencia/auto-applier-bot.git
cd auto-applier-bot

cp .env.example .env
# Edit .env — see Configuration section below
```

#### Personal data files

The bot needs four files with your personal information. These are git-ignored to keep sensitive data out of the repo. Copy the `.example` versions and fill in your own details:

```bash
cp data/answers.example.yaml         data/answers.yaml
cp data/profile_context.example.yaml data/profile_context.yaml
cp data/cvs/cv_remoto.example.json   data/cvs/cv_remoto.json
cp data/cvs/cv_local.example.json    data/cvs/cv_local.json
```

| File | Purpose |
|------|---------|
| `data/answers.yaml` | Pre-defined answers for form fields — name, email, phone, salary, DNI, etc. Zero LLM cost. |
| `data/profile_context.yaml` | Narrative context injected into LLM prompts for open-ended HR questions |
| `data/cvs/cv_remoto.json` | CV profile for remote tech roles (LinkedIn, RemoteOK, WeWorkRemotely) |
| `data/cvs/cv_local.json` | CV profile for local non-tech roles (Computrabajo, Indeed, ZonaJobs, Bumeran) |

Add your CV PDFs to `data/cvs/` — they're git-ignored too.

### 2 · Start services

```bash
docker-compose up -d
```

This starts three containers: `auto_applier_bot`, `auto_applier_db` (PostgreSQL), and `auto_applier_dashboard`.

### 3 · Initialise the database

```bash
docker-compose exec bot alembic upgrade head
docker-compose exec bot python main.py seed
```

### 4 · LinkedIn login (one-time manual step)

LinkedIn requires real Chrome to bypass WAF detection. Run this once:

```bash
docker-compose exec bot python login_helper.py --platform linkedin
```

A Chrome window will open — log in manually (including 2FA if prompted). The bot saves your session cookies and won't ask again until they expire (~1 year).

### 5 · Run

```bash
# Run the pipeline once right now
docker-compose exec bot python main.py run

# Or let the scheduler handle it (08:00 and 20:00 daily)
docker-compose exec bot python main.py schedule
```

### 6 · Open the dashboard

```
http://localhost:8080
```

---

## Configuration

Copy `.env.example` to `.env` and fill in the required values:

| Variable | Required | Description |
|----------|----------|-------------|
| `DB_URL` | ✅ | PostgreSQL connection string (pre-filled for Docker Compose) |
| `GEMINI_API_KEY` | ✅ | Google Gemini API key |
| `TELEGRAM_BOT_TOKEN` | ✓ recommended | Telegram bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | ✓ recommended | Your Telegram chat ID |
| `CHROME_EXECUTABLE_PATH` | ✅ | Full path to your Chrome binary (for LinkedIn) |
| `OLLAMA_URL` | optional | Ollama host — defaults to `http://host.docker.internal:11434` |
| `GMAIL_ADDRESS` | optional | Gmail address for reply monitoring |
| `GMAIL_APP_PASSWORD` | optional | [Gmail App Password](https://myaccount.google.com/apppasswords) (not your regular password) |
| `MAX_APPLICATIONS_PER_DAY_LINKEDIN` | optional | Safety cap — defaults to 5 |

---

## Dashboard

The web dashboard runs at `http://localhost:8080` and shows:

- **Stats cards** — applications sent today, jobs scraped, AI match rate, success rate
- **14-day bar chart** — applied vs failed per day
- **Platform grid** — live status, daily progress (e.g. `7/15`), circuit breaker alerts
- **Applications table** — searchable, filterable by status, with score and direct link to each listing

---

## How form filling works

The bot never blocks on an unknown form field. For each field in a job application:

1. **YAML exact match** — `data/answers.yaml` has 80+ pre-defined answers (name, salary, availability, cover letter, etc.) — answered at zero LLM cost
2. **Fuzzy match** — normalised key matching catches variants of the same question
3. **LLM** — Ollama (local, free) → Gemini fallback. The LLM receives the full CV JSON plus `data/profile_context.yaml`, a narrative document with detailed answers to behavioral, motivational, and situational HR questions
4. **Auto-save** — short LLM answers (≤ 150 chars) are saved back to `answers.yaml`, making the next identical question free

---

## Two CV profiles

The bot maintains two separate CVs targeting different job markets:

| Profile | Target | Platforms |
|---------|--------|-----------|
| `remoto` | Remote tech roles (Full Stack / Backend / Frontend) | LinkedIn, RemoteOK, WeWorkRemotely |
| `local` | In-person non-tech roles in Río Cuarto, Argentina | Computrabajo, Indeed, ZonaJobs, Bumeran |

Each platform automatically uses the right profile. For platforms like LinkedIn where both remote and in-person roles appear, the bot selects the profile based on the job's modality.

---

## Project structure

```
auto_applier_bot/
├── ai_engine/
│   ├── context_cache.py      # Gemini context caching for CV
│   ├── cv_loader.py          # CV JSON reader
│   ├── form_filler.py        # Form field answerer (YAML → LLM)
│   └── job_matcher.py        # CV↔job AI scorer
├── core/
│   ├── config.py             # Pydantic settings
│   ├── database.py           # SQLAlchemy engine + session
│   ├── enums.py              # Status enums
│   └── models.py             # ORM models
├── dashboard/
│   ├── main.py               # FastAPI backend (read-only API)
│   ├── static/index.html     # Single-page frontend
│   └── Dockerfile
├── data/
│   ├── answers.yaml          # Pre-defined form answers (zero LLM cost)
│   ├── profile_context.yaml  # Narrative context for LLM form filling
│   └── cvs/                  # CV JSON + PDF files
├── migrations/               # Alembic versions
├── orchestrator/
│   ├── lock_manager.py       # Prevents concurrent Playwright instances
│   ├── pipeline.py           # Full scrape→match→apply→report flow
│   └── scheduler.py          # APScheduler (pipeline 2x/day + email every 2h)
├── scrapers/                 # One file per platform
├── services/
│   ├── applier.py            # BaseApplier + circuit breaker + queue runner
│   ├── appliers/             # Platform-specific applier implementations
│   ├── email_monitor.py      # Gmail IMAP reply monitor
│   ├── notifier.py           # Telegram notifications
│   ├── screenshot.py         # Error screenshot capture
│   ├── session_manager.py    # Cookie storage (Fernet-encrypted)
│   └── telegram_bot.py       # Inline keyboard handler for REVIEW_SCORE
├── tests/                    # 115 tests across all modules
├── docker-compose.yml
├── Dockerfile
├── main.py                   # CLI entry point
└── requirements.txt
```

---

## Running tests

```bash
docker-compose exec bot pytest tests/ -q
```

---

## Security notes

- `data/cookies/` is git-ignored and Fernet-encrypted at rest
- LinkedIn sessions are always established from your real residential IP — never proxied
- The circuit breaker pauses a platform for 24h on CAPTCHA detection
- `lock_manager.py` prevents concurrent Playwright instances (each uses ~1.5 GB RAM)

---

## Detailed setup guide

For Windows-specific setup (WSL2, Docker Desktop configuration, LinkedIn cookie management, Gmail App Password), see [`documentacion_setup.md`](./documentacion_setup.md).

---

## License

MIT — feel free to fork and adapt for your own job search.

---

*Built by [Tiago Frencia](https://tiago-frencia.vercel.app/) — [GitHub](https://github.com/TiagoFrencia) · [LinkedIn](https://www.linkedin.com/in/tiagofrencia)*
