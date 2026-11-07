#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gmail_tools.py
==============
Phase 2 of the "always-on prospecting" system: actually SENDING an already-
approved outreach draft via the user's own Gmail/Workspace account -- per
the explicit decision recorded in the session recap ("email sending: the
user's own Gmail/Google Workspace account, via Gmail API + OAuth, not a
transactional email service"). Phase 1 (drafting + the /review approve-
queue) already existed; this module is what makes clicking "Approve" in
/review actually send the email, once you've connected Gmail once.

No new Python packages are required -- this talks to Google's OAuth and
Gmail REST endpoints directly with `requests` (already a dependency), on
purpose, so you don't have to add google-auth/google-api-python-client to
get Phase 2 working. If you'd rather use Google's official client libraries
later, this module's three functions (build_auth_url, exchange_code_for_
tokens, refresh_access_token, send_gmail) are exactly what you'd replace.

-----------------------------------------------------------------------
ONE-TIME SETUP (Google Cloud Console) -- do this before anything here works
-----------------------------------------------------------------------
1. Go to https://console.cloud.google.com/ -> create a new project (or pick
   an existing one) -- any name, e.g. "kamado-prospecting".
2. Left sidebar -> "APIs & Services" -> "Library" -> search "Gmail API" ->
   Enable.
3. "APIs & Services" -> "OAuth consent screen":
     - User type: External (this works fine even though you're the only
       user -- it just means Google shows an "unverified app" warning on
       login, which is expected and safe to click through, see step 6).
     - App name / support email / developer email: anything, it's only
       shown to you.
     - Scopes: click "Add or remove scopes" -> add
       https://www.googleapis.com/auth/gmail.send (search "gmail.send").
       That's the ONLY scope Phase 2 needs -- it can send mail as you, it
       CANNOT read your inbox. (Phase 3, reading replies + Calendar, will
       need broader scopes added later -- that'll be a separate, explicit
       re-consent step, not silently expanded here.)
     - Test users: add your own Gmail address here. Required while the app
       is in "Testing" mode (the default, and fine to stay in forever for
       personal use -- publishing/verification is only needed if OTHER
       people besides you will use this app).
4. "APIs & Services" -> "Credentials" -> "Create Credentials" -> "OAuth
   client ID":
     - Application type: "Web application" (NOT "Desktop app" -- this app
       is a FastAPI web server with its own callback route, not a CLI tool).
     - Name: anything.
     - Authorized redirect URIs -> Add URI:
         http://localhost:8000/auth/gmail/callback
       (match this exactly to GOOGLE_REDIRECT_URI in your .env below --
       add a second URI here later for your real domain once this is
       actually deployed somewhere other than your own machine.)
     - Create. Copy the "Client ID" and "Client secret" it shows you.
5. Put these in your .env (see .env.example):
       GOOGLE_CLIENT_ID=<the client id>.apps.googleusercontent.com
       GOOGLE_CLIENT_SECRET=<the client secret>
       GOOGLE_REDIRECT_URI=http://localhost:8000/auth/gmail/callback
6. Run the app (`uvicorn main:app --reload`), then in your browser visit:
       http://localhost:8000/auth/gmail/start
   Sign in with the Gmail/Workspace account you want to send FROM. Google
   will show an "unverified app" interstitial -- click "Advanced" -> "Go
   to <your app name> (unsafe)" -- this is expected for an app only you
   use, not a real safety warning. Approve the gmail.send permission.
   You'll land back on /auth/gmail/callback, which stores a refresh token
   in Postgres (a new `gmail_oauth_tokens` table, auto-created on startup
   the same way seen_companies/outreach_drafts are) -- one-time; it
   survives app restarts, so you only do this once per Google account
   (unless you revoke access, see below).
7. Confirm it worked: GET /auth/gmail/status should return
   {"connected": true, "email": "you@yourdomain.com"}.

To disconnect / reconnect a different account: POST /auth/gmail/disconnect
(or revoke access directly at https://myaccount.google.com/permissions),
then repeat step 6.

Token lifetime: the REFRESH token Google issues here doesn't expire on its
own (it can be invalidated by you revoking access, changing your Google
password, or ~6 months of the app being unused) -- ACCESS tokens expire
hourly and are refreshed automatically by _get_valid_gmail_access_token()
in main.py before every send, so in normal use you never have to redo the
browser flow.
"""
import base64
import os
from email.mime.text import MIMEText
from typing import Optional
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv
load_dotenv()

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "http://localhost:8000/auth/gmail/callback")

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"
GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"

# Least-privilege scope for Phase 2 -- send only, can't read the inbox.
GMAIL_SCOPES = "https://www.googleapis.com/auth/gmail.send"


def gmail_configured() -> bool:
    """True once GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET are set in .env --
    i.e. step 4/5 of the setup above has been done. /auth/gmail/start
    checks this before redirecting anywhere, so a missing .env value fails
    with a clear message instead of a confusing Google error page."""
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)


def build_auth_url(state: str = "") -> str:
    """Builds the URL to send the browser to for the Google consent screen.
    access_type=offline + prompt=consent together are what make Google
    actually hand back a refresh_token (without prompt=consent, a user who
    has already granted access before -- e.g. testing this twice -- may not
    get a refresh_token on the second exchange, only an access_token)."""
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": GMAIL_SCOPES,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


def exchange_code_for_tokens(code: str) -> dict:
    """Exchanges the one-time `code` /auth/gmail/callback receives for a
    real access_token + refresh_token. Raises requests.RequestException on
    failure (bad/expired code, redirect_uri mismatch, wrong client secret)
    -- the caller (main.py's callback route) is expected to catch this and
    show a clear error rather than a raw traceback."""
    resp = requests.post(
        GOOGLE_TOKEN_URL,
        data={
            "code": code,
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri": GOOGLE_REDIRECT_URI,
            "grant_type": "authorization_code",
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()  # {"access_token", "refresh_token", "expires_in", "scope", "token_type", "id_token"}


def refresh_access_token(refresh_token: str) -> dict:
    """Trades a stored refresh_token for a fresh access_token (no new
    refresh_token comes back from this call -- the original one keeps
    working indefinitely, see the module docstring). Raises
    requests.RequestException on failure -- e.g. the refresh_token was
    revoked, which the caller should treat as "gmail disconnected, prompt
    the user to /auth/gmail/start again", not a transient error."""
    resp = requests.post(
        GOOGLE_TOKEN_URL,
        data={
            "refresh_token": refresh_token,
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "grant_type": "refresh_token",
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()  # {"access_token", "expires_in", "scope", "token_type"}


def get_user_email(access_token: str) -> Optional[str]:
    """Just for display (/auth/gmail/status, the /review page banner) --
    which Google account is actually connected. Never raises -- returns
    None on any failure, since not knowing the email shouldn't block
    sending."""
    try:
        resp = requests.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json().get("email")
    except requests.RequestException:
        return None


def send_gmail(access_token: str, to_email: str, subject: str, body: str) -> dict:
    """
    Sends ONE plain-text email via the Gmail API, from whichever account
    authorized `access_token` -- this is the actual "Phase 2" action:
    turning an approved draft into a real sent email.

    Never raises -- any failure (expired token, invalid recipient, Gmail
    API error) returns {"ok": False, "error": "..."} so the caller
    (main.py's /outreach/{id}/approve) can record why a send failed on the
    draft row instead of crashing the whole request.

    Returns:
        {"ok": True, "message_id": "..."} on success, or
        {"ok": False, "error": "..."} on failure.
    """
    try:
        msg = MIMEText(body)
        msg["to"] = to_email
        msg["subject"] = subject
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
        resp = requests.post(
            GMAIL_SEND_URL,
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            json={"raw": raw},
            timeout=20,
        )
        resp.raise_for_status()
        return {"ok": True, "message_id": resp.json().get("id")}
    except requests.RequestException as e:
        resp_obj = getattr(e, "response", None)
        detail = (getattr(resp_obj, "text", "") or "").strip() or str(e)
        return {"ok": False, "error": detail[:500]}