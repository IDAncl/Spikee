# Spikee

Spikee is a B2B cold-outreach bot with a web dashboard. It finds companies
and the right people at them, verifies their contact details, drafts a
personalized email or message for each one based on real facts about the
company, and (by default) waits for a human to approve every message
before it goes out.

It's one process: `ui_app.py` is a Flask app that serves the dashboard
*and* runs the background scan loop in the same process.

## What it does

- Finds target companies and contacts and checks that their details are
  current (`outreach_bot.py`).
- Enriches company/person data from LinkedIn and other sources
  (`enrich_from_linkedin.py`).
- Drafts outreach emails with an LLM (Claude / Gemini / Groq, configurable)
  grounded in real facts about the target.
- Holds drafts for human approval before sending, unless review mode is
  turned off.
- Sends approved emails through Gmail and logs every send, reply, and
  meeting booked.
- Books meetings through either Google Calendar or Calendly.
- Tracks everything (leads, pipeline stage, activity log) in Supabase.
- Serves a dashboard (`ui_app.py`) showing drafts awaiting approval, sent
  messages, people who booked, items needing attention, and pipeline
  stats.

## Project layout

```
outreach_bot.py     Core scan/enrich/draft/send loop and scheduler
ui_app.py            Flask dashboard (the deployed entry point)
gmail_tools.py        Gmail send + OAuth helpers
enrich_from_linkedin.py   LinkedIn enrichment
check_hyphen_emails.py    One-off data-quality script
*_templete.txt         Email copy templates (company / personal)
Icp.txt                 Ideal customer profile used to steer targeting
lessons.txt             Running notes the bot's learning loop writes to
requirements.txt        Python dependencies
Procfile                Process command for Railway/Heroku-style hosts
docker-compose.yml       Local Crawl4AI sidecar for scraping
```

`ui_app.py.bak_pretheme` is a working draft of an alternate dashboard
theme, kept alongside the live file while that redesign is in progress.

## Setup

1. Python 3.11 (see `.python-version`).
2. `pip install -r requirements.txt`
3. Copy `.env.example` to `.env` and fill in real values (see below).
4. `python ui_app.py` to run the dashboard + scan loop locally.

## Configuration

All configuration lives in environment variables - see `.env.example`
for the full list with comments on what's required vs. optional. At a
minimum you need:

- `SUPABASE_URL`, `SUPABASE_KEY` - where leads and activity are stored
- `GMAIL_USER`, `GMAIL_APP_PASSWORD` - the sending mailbox
- `ANTHROPIC_API_KEY` (or a Gemini/Groq key, via `LLM_PROVIDER`) - drafts the
  outreach copy

Google Calendar booking needs `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`,
`GOOGLE_REDIRECT_URI`, plus a local `credentials.json` / `token.json`
(never committed - see `.gitignore`). Using Calendly instead only needs
`CALENDLY_LINK` and `CALENDLY_API_TOKEN`.

## Deploying

See [`DEPLOY.md`](DEPLOY.md) for step-by-step Railway deployment,
including how to get `credentials.json`/`token.json` onto a host without
committing them.

## License

MIT - see [`LICENSE`](LICENSE).
