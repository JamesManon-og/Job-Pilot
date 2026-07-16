# JobPilot — Local AI Job Application Automation

A fully local system that scrapes jobs, scores them against your resume with a local LLM
(Ollama), generates application materials, autofills applications via Playwright, and logs
everything — with a Next.js dashboard. Nothing leaves your machine.

## Status

| Milestone | Description | Status |
|---|---|---|
| M1 | Project architecture & scaffold | ✅ done |
| M2 | Database layer (SQLAlchemy + Alembic + repositories) | ✅ done |
| M3 | Scraper framework (rate limiter, normalizer) | ⏳ next |
| M4 | RemoteOK scraper | planned |
| M5 | PDF resume parser | planned |
| M6 | Ollama LLM integration | planned |
| M7 | Matching engine (LLM scoring + weighted ranking) | planned |
| M8 | FastAPI + Next.js dashboard | planned |
| M9 | Playwright autofill | planned |
| M10 | Application logger | planned |
| M11 | Human-approval workflow | planned |
| M12 | Production hardening | planned |

## Scope boundaries

- **Human Approval mode is the default.** Auto-submit is opt-in and always capped by
  `max_applications_per_day`.
- **Not included, by design:** CAPTCHA solving/bypass and browser-fingerprint rotation
  (bot-detection evasion). LinkedIn/Indeed scrapers, when added, will run through your own
  logged-in session, rate-limited, and are documented as ToS-risky.

## Architecture

Clean Architecture. The domain layer (`jobpilot/domain`) is pure — entities and enums with
zero infrastructure imports. Everything else depends inward:

```
backend/src/jobpilot/
├── config/          Settings (.env) + UserPreferences (config/config.yaml)
├── domain/          Pure entities & enums (Job, Application, MatchResult, …)
├── database/        Async SQLAlchemy engine, ORM tables, repositories
│   └── repositories/  JobRepository, ResumeRepository, ApplicationRepository, ScrapeRunRepository
├── scrapers/        BaseScraper ABC + plugin registry (framework in M3)
├── llm/             LLMProvider protocol (Ollama client in M6)
├── matcher/         Matching engine (M7)
├── api/             FastAPI app (routers land in M8)
├── autofill/        Playwright autofill (M9)
├── applications/    Application logging & approval workflow (M10–M11)
└── logging_setup.py Rich console + rotating file logs
```

Key design decisions:

- **Plugin scrapers** — each board subclasses `BaseScraper` and self-registers via
  `@register_scraper`; adding a board is one new module, nothing else changes.
- **Repository pattern** — services never touch SQLAlchemy directly; repositories translate
  between ORM rows and domain models. Tests inject a real repository over a temp SQLite DB.
- **Duplicate prevention at the schema level** — `jobs.dedup_hash`
  (sha256 of normalized company+title+url) is UNIQUE, so re-scrapes upsert instead of
  duplicating; `applications.job_id` is UNIQUE, so applying twice to the same job is
  impossible even if application code has a bug. `ApplicationRepository` additionally
  checks company+position across sources.
- **LLM behind a protocol** — `LLMProvider` is a structural type; Ollama is one
  implementation, tests use fakes, and the model can be swapped via config.
- **Enums stored as strings** — keeps SQLite schemas and Alembic migrations trivial;
  repositories convert to typed enums at the boundary.

## Setup

Requires Python 3.12+.

```bash
cd backend
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

Optional: copy `backend/.env.example` to `backend/.env` and adjust. Preferences live in
`config/config.yaml` (thresholds, blacklist, approval mode, daily cap, …).

## Run

```bash
cd backend
.venv/bin/python -m jobpilot db init      # create/upgrade the database (Alembic)
.venv/bin/python -m jobpilot db stats     # row counts per table
.venv/bin/python -m jobpilot config show  # resolved settings + preferences
```

## Develop

```bash
cd backend
.venv/bin/python -m pytest        # 30 tests: unit (config, domain) + integration (repositories)
.venv/bin/ruff check .            # lint
.venv/bin/ruff format .           # format
.venv/bin/mypy                    # strict type checking
.venv/bin/alembic revision --autogenerate -m "…"   # new migration after ORM changes
```

The database lives at `data/jobpilot.db` (gitignored). Logs go to `logs/jobpilot.log`.
