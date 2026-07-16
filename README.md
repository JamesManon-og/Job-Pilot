# JobPilot — Local AI Job Application Automation

A fully local system that scrapes jobs, scores them against your resume with a local LLM
(Ollama), generates application materials, autofills forms via Playwright, and logs
everything — with a Next.js dashboard. Nothing leaves your machine.

## Status

| Milestone | Description | Status |
|---|---|---|
| M1 | Project architecture & scaffold | done |
| M2 | Database layer (SQLAlchemy + Alembic + repositories) | done |
| M3 | Scraper framework (rate limiter, normalizer, runner) | done |
| M4 | RemoteOK scraper | done |
| M5 | PDF resume parser | done |
| M6 | Ollama LLM integration | done |
| M7 | Matching engine (LLM scoring + weighted ranking) | done |
| M8 | FastAPI + Next.js dashboard | done |
| M9 | Playwright autofill | done |
| M10 | Application service & orchestration | done |
| M11 | Human-approval workflow | done |
| M12 | Production hardening, pipeline, Dockerfile | done |

## Quick Start

```bash
# 1. Backend
cd backend
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m jobpilot db init
.venv/bin/playwright install chromium

# 2. Ollama (macOS)
brew install ollama
brew services start ollama
ollama pull qwen3:8b

# 3. Import your resume
.venv/bin/python -m jobpilot resume import ~/path/to/resume.pdf

# 4. Run the full pipeline
.venv/bin/python -m jobpilot run

# 5. Frontend (requires Node 18+)
cd ../frontend
npm install
npm run dev        # http://localhost:3000

# 6. Dashboard API
cd ../backend
.venv/bin/python -m jobpilot serve   # http://localhost:8000
```

## CLI Commands

```
jobpilot db init                    Create/upgrade the database (Alembic)
jobpilot db stats                   Print row counts per table
jobpilot config show                Print resolved settings + preferences
jobpilot scrape [--source remoteok] Scrape job boards (default: all)
jobpilot resume import <path>       Parse and store a resume PDF
jobpilot resume list                List stored resume versions
jobpilot llm check                  Verify Ollama server, model, generation
jobpilot match [--job-id N]         Score jobs against the active resume
jobpilot rank [--top 20]            Show ranked matches
jobpilot apply <job_id>             Prepare an application for a job
jobpilot run [--top 5]              Full pipeline: scrape → match → rank → prepare
jobpilot serve [--port 8000]        Start the dashboard API server
```

## Pipeline (`jobpilot run`)

The `run` command executes the full pipeline in one shot:

1. **Scrape** — pulls new jobs from all configured sources (RemoteOK by default)
2. **Match** — scores unscored jobs against your resume using the local LLM
3. **Rank** — sorts by composite score (LLM score + salary + remote + recency + tech overlap)
4. **Prepare** — creates applications for the top N matches (default 5), generates cover letters and pre-fills answers

When `human_approval_enabled` is true (the default), prepared applications land in the
Review Queue. Open the dashboard, review each one, edit the cover letter if needed, then
click **Approve** to trigger submission via Playwright, or **Reject** to discard.

## Configuration

**Settings** — environment variables or `backend/.env`:

| Variable | Default | Description |
|---|---|---|
| `JOBPILOT_DATA_DIR` | `data/` | Database, screenshots, resumes |
| `JOBPILOT_OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama server |
| `JOBPILOT_OLLAMA_MODEL` | `qwen3:8b` | LLM model name |
| `JOBPILOT_LOG_LEVEL` | `INFO` | Logging verbosity |

**Preferences** — `config/config.yaml`:

```yaml
min_match_score: 75
preferred_technologies: [python, react, typescript]
remote_only: true
blacklist_companies: [SpamCorp]
max_applications_per_day: 10
auto_submit_enabled: false
human_approval_enabled: true

applicant:
  name: Your Name
  email: you@example.com
  phone: "+1-555-0100"
  github_url: https://github.com/you
  linkedin_url: https://linkedin.com/in/you
  resume_file: data/resumes/resume.pdf

ranking_weights:
  resume_match: 0.55
  salary: 0.10
  remote: 0.10
  recency: 0.10
  tech_overlap: 0.15
```

## Architecture

Clean Architecture — the domain layer is pure (no infrastructure imports), everything
depends inward.

```
backend/src/jobpilot/
├── config/          Settings (.env) + UserPreferences (config.yaml)
├── domain/          Pure entities & enums (Job, Application, MatchResult, …)
├── database/        Async SQLAlchemy engine, ORM, Alembic migrations
│   └── repositories/  JobRepo, ResumeRepo, ApplicationRepo, ScrapeRunRepo, MatchResultRepo
├── scrapers/        BaseScraper ABC + plugin registry + RemoteOK
├── llm/             LLMProvider protocol + OllamaClient
├── matcher/         MatchEngine (LLM scoring) + composite ranking
├── resume/          PDF extraction + heuristic/LLM structuring
├── api/             FastAPI routers (stats, jobs, matches, applications, review, actions)
├── autofill/        Playwright autofill engine (never clicks submit)
├── applications/    ApplicationService (prepare, submit, daily cap, dedup)
└── logging_setup.py Rich console + rotating file logs

frontend/
├── src/app/         Next.js pages (overview, jobs, matches, applications, review)
├── src/components/  Shared UI components
└── src/lib/api.ts   Typed API client
```

Key design decisions:

- **Plugin scrapers** — subclass `BaseScraper` + `@register_scraper`; adding a board is one file.
- **Repository pattern** — services never touch SQLAlchemy directly.
- **Duplicate prevention** — `jobs.dedup_hash` UNIQUE, `applications.job_id` UNIQUE,
  plus cross-source company+title check.
- **LLM behind a protocol** — `LLMProvider` is structural; Ollama is one implementation,
  tests use fakes.
- **Autofill safety** — the engine NEVER clicks submit. Submission only happens after
  explicit human approval (or opt-in auto-submit with daily cap).

## Scope Boundaries

- **Human Approval is the default.** Auto-submit is opt-in and always capped.
- **Not included by design:** CAPTCHA solving/bypass, browser-fingerprint rotation,
  proxy rotation for evasion.
- LinkedIn/Indeed scrapers are documented as ToS-risky.

## Docker (Backend Only)

```bash
docker build -t jobpilot-backend .
docker run -p 8000:8000 -v $(pwd)/data:/app/data -v $(pwd)/config:/app/config jobpilot-backend
```

The container runs the API server. Ollama must be reachable from the container
(use `--network host` on Linux, or set `JOBPILOT_OLLAMA_BASE_URL` to your host IP).

## Development

```bash
cd backend
.venv/bin/python -m pytest                    # ~90 tests
.venv/bin/ruff check src/ tests/              # lint
.venv/bin/ruff format --check src/ tests/     # format check
.venv/bin/mypy                                # strict type checking
```

## Troubleshooting

- **"No module named jobpilot"** — ensure you're using the venv Python, not a conda base.
  The venv must be created with `python3.12 -m venv .venv` from python.org or Homebrew,
  not from miniconda (its site.py skips editable-install `.pth` files).
- **Node version too old for Next.js** — use Node 18+ (`nvm use 22` if you have nvm).
- **Ollama not responding** — run `brew services start ollama`, then `ollama pull qwen3:8b`.
- **Playwright browser missing** — run `.venv/bin/playwright install chromium`.
