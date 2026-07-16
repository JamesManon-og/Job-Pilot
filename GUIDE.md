# JobPilot — User Guide

Everything runs on your machine. No data leaves it.

## What it does

JobPilot finds jobs, scores them against your resume with a local AI (Ollama),
writes cover letters, and fills out application forms for you. **You approve
every application before it's sent** — nothing is submitted automatically.

## What was built

| Part | What it is |
|---|---|
| **Scraper** | Pulls jobs from RemoteOK (more boards can be added as plugins) |
| **Resume parser** | Reads your PDF resume and extracts skills, tech, experience |
| **Matcher** | Local LLM scores each job 0–100 against your resume, then ranks them |
| **Application service** | Generates cover letters + answers, enforces daily cap and duplicate blocking |
| **Autofill** | Playwright fills the application form — it **never clicks submit** on its own |
| **Dashboard** | Web UI (localhost:3000) to browse jobs, matches, and review applications |
| **Review queue** | Where you edit, approve, or reject each application before it goes out |

## One-time setup

```bash
# Backend (from the project root)
cd backend
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m jobpilot db init
.venv/bin/playwright install chromium

# Local AI
brew install ollama
brew services start ollama
ollama pull qwen3:8b

# Your resume
.venv/bin/python -m jobpilot resume import ~/Documents/resume.pdf

# Your info for autofill — edit config/config.yaml, fill in the `applicant:` section
```

## Daily use

```bash
cd backend
.venv/bin/python -m jobpilot run          # scrape → score → rank → prepare top 5
.venv/bin/python -m jobpilot serve        # start the API (localhost:8000)
```

In a second terminal:

```bash
cd frontend && npm run dev                # dashboard at localhost:3000
```

Then open **localhost:3000 → Review**: read each prepared application, edit the
cover letter if you want, and click **Approve & Submit** (fills and submits the
form) or **Reject**.

## Useful commands

```bash
jobpilot llm check        # is Ollama working?
jobpilot db stats         # how much data do I have?
jobpilot rank --top 20    # show my best matches
jobpilot apply <job_id>   # prepare one specific job
jobpilot config show      # see all current settings
```

(All run as `.venv/bin/python -m jobpilot …` from `backend/`.)

## Key settings (`config/config.yaml`)

- `min_match_score: 75` — ignore jobs scoring below this
- `max_applications_per_day: 10` — hard cap, always enforced
- `human_approval_enabled: true` — keep this on; you review everything
- `blacklist_companies: [...]` — never apply to these
- `applicant:` — your name/email/phone/links used for autofill

## If something breaks

| Problem | Fix |
|---|---|
| "No module named jobpilot" | Use `.venv/bin/python`, not conda's python |
| Ollama errors | `brew services start ollama` then `ollama pull qwen3:8b` |
| Frontend won't start | Need Node 18+: `nvm use 22` |
| Browser missing | `.venv/bin/playwright install chromium` |
| Anything else | Check `logs/jobpilot.log` |

## Safety guarantees

- Applying twice to the same job is impossible (blocked at the database level).
- The autofill engine cannot click submit — submission only happens after your approval.
- Daily application cap is always enforced, even in auto mode.
- No CAPTCHA bypass, no bot-detection evasion — by design.
