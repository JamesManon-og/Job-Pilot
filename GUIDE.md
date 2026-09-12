# JobPilot — User Guide

Everything runs on your machine. No data leaves it.

## What it does

JobPilot finds jobs, scores them against your resume with a local AI (Ollama),
writes cover letters, and fills out application forms for you. **You approve
every application, and you click submit yourself** — JobPilot never submits.

## What was built

| Part | What it is |
|---|---|
| **Scraper** | Pulls jobs from RemoteOK (more boards can be added as plugins) |
| **Resume parser** | Reads your PDF resume and extracts skills, tech, experience |
| **Matcher** | Local LLM scores each job 0–100 against your resume, then ranks them |
| **Application service** | Generates cover letters + answers, enforces daily cap and duplicate blocking |
| **Autofill** | Opens approved applications in a browser and fills what it can identify — it **never clicks submit** |
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

# Your info for autofill (config.yaml is gitignored)
cp ../config/config.example.yaml ../config/config.yaml   # fill in `applicant:`
```

## Daily use

```bash
cd backend
.venv/bin/python -m jobpilot run          # scrape → score → rank → prepare top 5
.venv/bin/python -m jobpilot review       # approve / reject / edit in the terminal
.venv/bin/python -m jobpilot apply        # open approved ones in a browser, autofilled
.venv/bin/python -m jobpilot serve        # optional: the dashboard API (localhost:8000)
```

In a second terminal:

```bash
cd frontend && npm run dev                # dashboard at localhost:3000
```

You can also review at **localhost:3000 → Review**: edit the cover letter, then
**Approve** or **Reject**. Approving doesn't send anything.

`jobpilot apply` opens each approved application in a browser window and fills
it in. It tells you what it left for you (unknown questions, sensitive fields,
CAPTCHAs). Check everything, click the site's submit button yourself, then
press `s` and confirm. That's the only way an application becomes "submitted".

## Useful commands

```bash
jobpilot llm check        # is Ollama working?
jobpilot db stats         # how much data do I have?
jobpilot rank --top 20    # show my best matches
jobpilot prepare <job_id> # prepare one specific job
jobpilot config show      # see all current settings
```

(All run as `.venv/bin/python -m jobpilot …` from `backend/`.)

## Key settings (`config/config.yaml`)

- `min_match_score: 75` — `run` only prepares jobs scoring at least this
- `max_applications_per_day: 10` — hard cap, always enforced
- `blacklist_companies: [...]` — never apply to these
- `applicant:` — your name/email/phone/links used for autofill

## If something breaks

| Problem | Fix |
|---|---|
| "No module named jobpilot" | iCloud hid the venv — see README Troubleshooting |
| Ollama errors | `brew services start ollama` then `ollama pull qwen3:8b` |
| Frontend won't start | `npm ci` in `frontend/`; needs Node 20+ |
| Browser missing | `.venv/bin/playwright install chromium` |
| Anything else | Check `logs/jobpilot.log` |

## Safety guarantees

- Applying twice to the same job is impossible (blocked at the database level).
- An application can't skip approval or be marked submitted twice — every status
  change is validated and atomic, and logged.
- The autofill engine cannot click submit, never guesses sensitive fields (IDs,
  birth date, gender, salary history…), and never ticks consent boxes.
- Daily application cap is always enforced, counted against your local day.
- No CAPTCHA/MFA bypass, no bot-detection evasion — JobPilot stops and tells you.
