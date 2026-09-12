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

# 3. Your details (config.yaml is gitignored — it holds personal info)
cp ../config/config.example.yaml ../config/config.yaml   # then fill in `applicant:`

# 4. Import your resume
.venv/bin/python -m jobpilot resume import ~/path/to/resume.pdf

# 5. Find, score, and prepare applications
.venv/bin/python -m jobpilot run

# 6. Review, then open approved ones in a browser and submit them yourself
.venv/bin/python -m jobpilot review
.venv/bin/python -m jobpilot apply

# 7. Frontend (requires Node 20+)
cd ../frontend
npm ci
npm run dev        # http://localhost:3000

# 8. Dashboard API
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
jobpilot prepare <job_id>           Generate materials for a job and queue it for review
jobpilot review                     Approve/reject/edit prepared applications in the terminal
jobpilot apply [application_id…]    Open approved applications in a browser and autofill them
jobpilot run [--top 5]              Full pipeline: scrape → match → rank → prepare
jobpilot serve [--port 8000]        Start the dashboard API server
```

`scrape`, `match`, `prepare`, and `run` share a cross-process lock: a second
one exits with code 3 instead of racing the first. Configuration errors exit
with code 2 and name the offending field.

## Pipeline (`jobpilot run`)

1. **Scrape** — pulls new jobs from every configured source; a failing source
   is recorded and the others still run
2. **Match** — scores unscored jobs with the local LLM; one bad reply is
   skipped and retried next run, and progress survives interruption
3. **Rank** — composite score (LLM score + salary + remote + recency + tech
   overlap), keeping only jobs with LLM score ≥ `min_match_score`
   (`--min-score` can raise it, never lower it) that have no application yet
4. **Prepare** — cover letter and answers for the top N, grounded in your
   resume, queued as `pending_review`

## Approval and submission

```
pending_review ──approve──▶ approved ──jobpilot apply──▶ awaiting_confirmation
                                                              │ you submit on the site,
                                                              ▼ then confirm
                                                          submitted
```

- **Approve** in the dashboard or `jobpilot review`. Approving only changes the
  status; nothing is opened or sent.
- **`jobpilot apply`** opens each approved application in a visible browser and
  autofills what it can identify. It lists what it couldn't fill (unknown
  questions, sensitive fields, CAPTCHAs, required checkboxes).
- **You** check the form and click the site's submit button, then confirm in
  the terminal. Only that confirmation records `submitted`.

Every transition is validated and atomic: an application can't skip approval,
be opened by two sessions, or be submitted twice. Each change is recorded in
`application_events`.

## Configuration

**Settings** — environment variables or `.env` (project root or `backend/`):

| Variable | Default | Description |
|---|---|---|
| `JOBPILOT_PROJECT_ROOT` | repo root | Base for the defaults below |
| `JOBPILOT_DATA_DIR` | `<root>/data` | Database, screenshots, locks |
| `JOBPILOT_PREFERENCES_PATH` | `<root>/config/config.yaml` | Preferences file |
| `JOBPILOT_LOGS_DIR` | `<root>/logs` | Rotating log files |
| `JOBPILOT_DATABASE_URL` | `sqlite+aiosqlite:///<data>/jobpilot.db` | Database |
| `JOBPILOT_OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama server |
| `JOBPILOT_OLLAMA_MODEL` | `qwen3:8b` | LLM model name |
| `JOBPILOT_LOG_LEVEL` | `INFO` | Logging verbosity |

**Preferences** — `config/config.yaml` (copy from `config/config.example.yaml`;
gitignored because it holds your personal details). See the example file for
every key. Relative `resume_file` paths resolve against the project root; if
it's blank, the active imported resume is uploaded.

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
├── applications/    ApplicationService (prepare, approve, claim, confirm) + `apply` runner
├── pipeline.py      `jobpilot run` orchestration
├── locks.py         Cross-process pipeline lock
└── logging_setup.py Rich console + rotating file logs

frontend/
├── src/app/         Next.js pages (overview, jobs, matches, applications, review)
├── src/components/  Shared UI components
└── src/lib/api.ts   Typed API client
```

Key design decisions:

- **Plugin scrapers** — subclass `BaseScraper` + `@register_scraper`; adding a board is one file.
- **Repository pattern** — services never touch SQLAlchemy directly.
- **Duplicate prevention** — jobs are keyed by `dedup_hash` with an `application_url`
  fallback; `applications.job_id` UNIQUE plus a cross-source company+title check.
- **Validated lifecycle** — `APPLICATION_TRANSITIONS` in the domain layer; every status
  change is a compare-and-set UPDATE.
- **LLM behind a protocol** — `LLMProvider` is structural; Ollama is one implementation,
  tests use fakes.
- **Autofill safety** — the engine never clicks any button, never guesses unknown or
  sensitive fields, never overwrites prefilled values, never ticks checkboxes, and stops
  at login/MFA/bot-check pages.

## Scope Boundaries

- **Human approval is mandatory**, and only you click submit.
- **Not included by design:** CAPTCHA solving/bypass, MFA handling, browser-fingerprint
  rotation, proxy rotation for evasion.
- LinkedIn/Indeed scrapers are documented as ToS-risky.

## Docker (Backend Only)

```bash
docker build -t jobpilot-backend .
docker run -p 127.0.0.1:8000:8000 -v $(pwd)/data:/app/data -v $(pwd)/config:/app/config jobpilot-backend
```

The container runs migrations, then the API server. Bind the port to `127.0.0.1`: the
API has no authentication, and a bare `-p 8000:8000` exposes it to your whole network.
Ollama must be reachable from the container (set `JOBPILOT_OLLAMA_BASE_URL`, e.g.
`http://host.docker.internal:11434`). `jobpilot apply` needs a visible browser, so run
it on the host.

## Development

```bash
cd backend
.venv/bin/python -m pytest                    # ~220 tests, incl. headless Playwright
.venv/bin/ruff check src/ tests/              # lint
.venv/bin/ruff format --check src/ tests/     # format check
.venv/bin/mypy                                # strict type checking
```

## Troubleshooting

- **"No module named jobpilot"** — if the repo is in an iCloud-synced folder
  (Desktop & Documents sync), iCloud flags `.venv` as hidden and Python ≥ 3.12.8
  then skips the editable-install `.pth` file. Keep the venv out of iCloud:
  `mv .venv .venv.nosync && ln -s .venv.nosync .venv && chflags -R nohidden .venv.nosync`.
  (Tests don't depend on it: pytest adds `src/` to the path.)
- **Frontend fails with `next: command not found`** — `node_modules` is out of sync;
  run `npm ci` in `frontend/`. Needs Node 20+.
- **Ollama not responding** — run `brew services start ollama`, then `ollama pull qwen3:8b`.
- **Playwright browser missing** — run `.venv/bin/playwright install chromium`.
- **"Another JobPilot pipeline run is already in progress"** — another scrape/match/run
  is running (the message has its pid). Locks are released automatically when a
  process exits, even if it crashed.
- **An application is stuck in `awaiting_confirmation`** — the next `jobpilot apply`
  asks whether you submitted it and records your answer.
