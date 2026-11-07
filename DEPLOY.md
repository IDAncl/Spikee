# Deploying to Railway

This app is one process: `ui_app.py` serves the dashboard AND runs the
scan loop in a background thread inside itself. Deploying it means
running that one process somewhere that keeps it alive continuously -
that's what Railway does.

## 1. Push this repo to GitHub

```bash
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin main
```

Before you do, double check secrets aren't tracked:

```bash
git ls-files | grep -iE "\.env$|credentials\.json|token\.json"
```

That should print nothing. If it prints something, stop and fix
`.gitignore` before pushing.

## 2. Create the Railway project

On railway.app: New Project -> Deploy from GitHub repo -> pick this
repo. Railway detects it's Python (via `requirements.txt`) and uses the
`Procfile` to know how to start it (`gunicorn ... ui_app:app`).

**Do not change the Procfile to use more than 1 worker.** The scan
loop's state (`scheduler_state`, `LEADS_LOCK`) lives in that one
process's memory - a second worker would be a second, independent
process running its own separate scan loop.

## 3. Set environment variables

In the Railway project -> Variables tab, add everything from
`.env.example` that isn't blank in your real `.env`, with the real
values:

- `SUPABASE_URL`, `SUPABASE_KEY`
- `GMAIL_USER`, `GMAIL_APP_PASSWORD`
- `ANTHROPIC_API_KEY`
- Everything else in `.env.example` is optional - only add what you've
  actually been using.

Railway sets `PORT` itself - don't add that one.

### Google Calendar's credentials.json / token.json

Real secrets, deliberately not in the repo. If you use the Google
Calendar booking flow (not Calendly), get them onto Railway as base64
env vars:

```powershell
# PowerShell, run locally wherever credentials.json/token.json exist
[Convert]::ToBase64String([IO.File]::ReadAllBytes("credentials.json"))
[Convert]::ToBase64String([IO.File]::ReadAllBytes("token.json"))
```

Paste each output as a Railway variable: `CREDENTIALS_JSON_B64` and
`TOKEN_JSON_B64`. `ui_app.py` decodes them back into real files the
first time it starts.

If you're using Calendly instead, skip this section entirely.

## 4. Deploy and check it

Railway deploys automatically once the repo + variables are set. Open
the generated `*.up.railway.app` URL - you should see the dashboard,
and the deploy logs should show something like:

```
Outreach bot scheduler [build ...] started - scans every N min, ... leads per scan.
```

If you don't see that line, check the logs for an import error first
(usually a missing env var).

From here on: every `git push` redeploys automatically.
