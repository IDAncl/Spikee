#!/usr/bin/env python3
"""
outreach_bot.py — the "mechanics" half of a simple cold-outreach loop.

This script does NOT write any email text itself. It only does the parts
that need to be reliable and repeatable:
  - pick the next batch of leads to contact
  - check whether an address looks "live" before sending (SMTP probe)
  - check the inbox for new replies from leads already contacted
  - actually send emails (via Gmail SMTP), threaded correctly
  - find open slots on your Google Calendar and book a meeting

The actual "what do I say" part (talking like the ICP's salesperson,
deciding if someone wants a meeting, picking a slot from their reply, or
noticing a specific day/time they proposed themselves - e.g. "free
tomorrow at 4?" - and checking the calendar for that exact time instead of
just handing them 3 generic options) is written by whichever LLM is
configured (LLM_PROVIDER in .env - Claude by default, Gemini/Groq also
supported) each time `run` fires, using icp.txt as the persona. This keeps
the script simple and means the persona/tone is easy to tune (just edit
icp.txt) without touching code.

Subcommands:
    run                        -> the one you actually schedule. Does everything:
                                  pull pending work, draft each email with the
                                  configured LLM using icp.txt as the persona, decide
                                  whether to offer/confirm a meeting slot, send,
                                  update your leads in Supabase. Point Windows Task
                                  Scheduler (or cron) at "python outreach_bot.py run"
                                  every 20 minutes and that's the whole loop.

                                  By default (REVIEW_MODE=true in .env), the
                                  first cold email to a brand-new lead is only
                                  DRAFTED, not sent - check activity_log.md or
                                  your leads in Supabase (status=draft_pending)
                                  and then run "approve" to actually send it.
                                  Replies to an ongoing conversation still send
                                  automatically (you're already in a live
                                  back-and-forth by then), but every send is
                                  logged either way.

    approve [--only EMAIL]    -> sends everything currently sitting as a
                                  draft (status=draft_pending). Run this by
                                  hand after reviewing activity_log.md.

    --- lower-level pieces `run` calls internally, useful for debugging ---
    pull                      -> pending.json: leads to contact + replies to answer
    send --file drafts.json   -> sends the drafted emails, updates your leads in Supabase
    slots [--count 3]         -> next N open 30-min slots on your calendar
    book --email E --start ISO --end ISO --company C --subject S
                               -> creates the calendar event + invites the lead
"""

import argparse
import email
import imaplib
import json
import os
import re
import smtplib
import socket
import sys
import threading
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from email.header import decode_header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import make_msgid, parseaddr
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    from supabase import create_client as _supabase_create_client
except ImportError:
    _supabase_create_client = None
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

# Leads now live in Supabase (Postgres), not leads.xlsx - see
# supabase_schema.sql for the table this expects, and
# migrate_leads_to_supabase.py for the one-time import of an existing
# leads.xlsx. Everything below (load_leads/save_leads/update_lead/
# add_lead_row) talks to Supabase; the rest of the bot (cmd_run,
# _handle_reply, approve/reject/dismiss, ui_app.py, ...) is unchanged -
# it only ever went through those four functions and the row dicts they
# return, never touched a spreadsheet directly.
SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").strip()
SUPABASE_KEY = (os.getenv("SUPABASE_KEY") or "").strip()
# Two separate tables now instead of one shared "leads" table - see the
# COMPANY_COLS/PERSON_COLS block below for exactly what's in each and
# supabase_schema.sql for the table definitions themselves.
#   company_emails: a company-level contact (no named person on file) -
#     always gets EMAIL_TEMPLATE_FILE's fixed pitch, personalized from a
#     website scrape+summary (company_research), same as this bot always
#     did before this split.
#   personal_emails: a named individual you're adding LinkedIn/other data
#     about yourself - always gets a PERSONA_TEMPLATES_FILE template
#     (never EMAIL_TEMPLATE_FILE's company pitch), personalized from
#     linkedin_info instead of a website scrape.
SUPABASE_COMPANY_TABLE = (os.getenv("SUPABASE_COMPANY_TABLE") or "company_emails").strip()
SUPABASE_PERSON_TABLE = (os.getenv("SUPABASE_PERSON_TABLE") or "personal_emails").strip()
STATE_FILE = BASE_DIR / "state.json"
PENDING_FILE = BASE_DIR / "pending.json"

GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "5"))
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
# Generous on purpose - a batch call (classify/compose several leads in one
# JSON response) needs a lot more room than a single-lead call ever did, and
# a response that runs out of room mid-generation comes back as truncated,
# invalid JSON (see _ask_gemini_raw()) rather than an obvious "ran out of
# tokens" error.
GEMINI_MAX_OUTPUT_TOKENS = int(os.getenv("GEMINI_MAX_OUTPUT_TOKENS", "16384"))
# Which LLM writes the emails. "claude" (default - uses your Anthropic API
# key), "gemini", or "groq". Nothing else in this file needs to change to
# switch providers - every composer function still just calls
# get_gemini_client()/ask_gemini(), whichever provider is actually behind
# them (the names stayed "gemini" for that reason, even though the default
# is now Claude).
LLM_PROVIDER = (os.getenv("LLM_PROVIDER") or "claude").strip().lower()
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
# Haiku, not Sonnet, by default: at the volume this bot runs at (a handful
# of emails every 20 minutes), Haiku is plenty for short, on-persona sales
# emails and costs a fraction of Sonnet - switch CLAUDE_MODEL in .env to a
# Sonnet ID if you want more nuanced writing and don't mind the cost.
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
# openai/gpt-oss-20b, not the bigger 120b: on Groq's free tier every
# mainstream chat model shares the SAME 30 RPM / 1,000 RPD / 8,000 TPM /
# 200,000 TPD caps - 120b gets no bigger a quota, it's just slower, so 20b
# burns through the (shared, tight) 8K-tokens-per-minute cap less quickly
# for the same prompt.
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")
ICP_FILE = BASE_DIR / os.getenv("ICP_FILE", "icp.txt")
# A strict, mostly-fixed first-email template - see DEFAULT_EMAIL_TEMPLATE
# and compose_first_email_from_template() below. If this file doesn't
# exist yet, cmd_run creates a starter one and stops so you can edit it.
EMAIL_TEMPLATE_FILE = BASE_DIR / os.getenv("EMAIL_TEMPLATE_FILE", "company_email_templete.txt")
# Optional: several persona-specific first-email templates (one per job
# title / industry you sell to), picked automatically per lead - see
# personal_email_templete.txt, _load_personas(), and compose_first_email_with_persona()
# below. If this file doesn't exist, or a lead doesn't match anything in
# it well, EMAIL_TEMPLATE_FILE above is used exactly as before - this is
# fully opt-in, nothing changes until you create the file.
PERSONA_TEMPLATES_FILE = BASE_DIR / os.getenv("PERSONA_TEMPLATES_FILE", "personal_email_templete.txt")
# Optional: a self-hosted Crawl4AI Docker server (https://github.com/unclecode/crawl4ai)
# for the company-research scrape step - e.g. CRAWL4AI_URL=http://localhost:11235.
# If unset, falls back to a plain requests+BeautifulSoup fetch instead.
CRAWL4AI_URL = (os.getenv("CRAWL4AI_URL") or "").strip().rstrip("/")
# Only needed if your Crawl4AI server has API-key auth turned on (newer
# Crawl4AI Docker images do this by default) - if unset, no Authorization
# header is sent, which is fine for a server running with auth disabled.
CRAWL4AI_API_TOKEN = (os.getenv("CRAWL4AI_API_TOKEN") or "").strip()
# When true (the default), the FIRST cold email to each new lead is drafted
# and saved for you to review/approve instead of sent immediately. Replies
# to an ongoing conversation still go out automatically (you already have
# a live back-and-forth by then) but everything is logged either way - see
# activity_log.md. Set REVIEW_MODE=false in .env once you trust it.
REVIEW_MODE = os.getenv("REVIEW_MODE", "true").strip().lower() not in ("false", "0", "no")
LOG_FILE = BASE_DIR / "activity_log.md"

# The language every AI-written email (first emails, replies, referral
# handoffs) is written in by default. Replies still mirror the lead's own
# language when they wrote in something else (see COMPOSER_SYSTEM below) -
# this only sets what's used when there's nothing to mirror yet (a first
# email) or as the default tone otherwise. Change to "English" in .env if
# you ever want that back.
EMAIL_LANGUAGE = (os.getenv("EMAIL_LANGUAGE") or "Hebrew").strip()

# When set, the "they want to meet" flow sends ONE reply with this Calendly
# link instead of the old propose/confirm-with-Google-Calendar back-and-forth
# - Calendly handles the actual booking UI, and poll_calendly_bookings()
# (called once per scan) checks Calendly's API for who's booked since last
# time and marks that lead "booked" in Supabase automatically. Leave unset
# to keep the old Google Calendar slot-offer flow instead.
CALENDLY_LINK = (os.getenv("CALENDLY_LINK") or "").strip()
CALENDLY_API_TOKEN = (os.getenv("CALENDLY_API_TOKEN") or "").strip()
CALENDLY_API_BASE = "https://api.calendly.com"

# Optional: Clearout (https://app.clearout.io) real email-verification API
# for the liveness check every new lead goes through before it's ever
# drafted/sent to (see is_probably_live() below). Get a key from your
# Clearout account -> API -> API Key. When this is set, it's tried FIRST
# and is normally much more accurate than the old MX/SMTP probe below
# (which many mail providers just silently accept-then-bounce-later on,
# and which residential/cloud IPs often can't even complete since port 25
# outbound is commonly blocked) - if it's unset, or the Clearout call
# itself fails for any reason (network, quota, bad key), this transparently
# falls back to that original MX/SMTP check instead, same as every other
# "don't let our own hiccup kill a lead" fallback in this file.
CLEAROUT_API_KEY = (os.getenv("CLEAROUT_API_KEY") or "").strip()
CLEAROUT_VERIFY_URL = "https://api.clearout.io/v2/email_verify/instant"
# How long (ms) Clearout's own engine is allowed to spend verifying ONE
# address before it gives up and returns whatever it has - passed straight
# through in the request body. Clearout's own max is 180000 (3 min); kept
# well under that by default so one slow mailbox can't stall an entire run.
CLEAROUT_TIMEOUT_MS = int(os.getenv("CLEAROUT_TIMEOUT_MS", "20000"))

# Testing convenience: when true, is_probably_live() below skips the real
# liveness check entirely (no Clearout call, no MX lookup, no SMTP probe)
# and treats every lead as live. Use this to test-drive the REST of the
# pipeline - research, role classification, persona matching, drafting -
# on a batch of test leads without burning Clearout credits or waiting on
# SMTP probes for addresses you already know are fake/test ones. Leave
# unset (or false) for any real run: with this on, leads get drafted (and,
# outside REVIEW_MODE, even sent to) without ever actually confirming the
# address is real.
SKIP_LIVENESS_CHECK = os.getenv("SKIP_LIVENESS_CHECK", "false").strip().lower() in ("true", "1", "yes")

# Role classification for personal_emails (see classify_role() and
# ROLE_KEYWORDS below) - implements the "Spikee" role-routing spec: every
# named contact's job_title is classified into one of 5 categories before
# it's ever composed to, so an HR contact (who's almost never the actual
# decision-maker on security cameras) gets routed differently from a CEO,
# founder, COO, or security manager instead of the same pitch either way.
#   0.75 (ROLE_KEYWORD_ACCEPT_THRESHOLD): a keyword-match score at or above
#     this is trusted as-is, no AI call needed (covers the large majority
#     of real job titles - "קב\"ט", "CEO", "COO" etc match instantly and
#     for free). Below it, one AI call classifies against the same 5
#     categories instead.
#   0.6 (ROLE_NEEDS_REVIEW_THRESHOLD): whatever the final confidence is
#     (keyword OR AI) - below this, the classification is too shaky to
#     route on, so role_category is stored as "needs_review" (for you to
#     glance at later) and the contact is treated exactly as it always was
#     before this feature existed (compose_person_email()'s normal
#     free-text persona pick, no HR routing, no length change) - never
#     blocked, never delayed, just not routed on an unreliable guess.
# Both are starting points the spec itself flags as needing real-world
# tuning after a week or two of runs - change via .env, no code edit needed.
ROLE_KEYWORD_ACCEPT_THRESHOLD = float(os.getenv("ROLE_KEYWORD_ACCEPT_THRESHOLD", "0.75"))
ROLE_NEEDS_REVIEW_THRESHOLD = float(os.getenv("ROLE_NEEDS_REVIEW_THRESHOLD", "0.6"))
# How many personal_emails rows (any status except dead/rejected) to
# (re)classify per run if they don't have a role_category stored yet - lets
# contacts added/contacted before this feature existed gradually get
# classified too, same bounded-backfill pattern as the company-research
# backfill above. Classification is cheap (keyword-only for most titles),
# so this can be generous.
MAX_ROLE_BACKFILL_PER_RUN = int(os.getenv("MAX_ROLE_BACKFILL_PER_RUN", "20"))

# Learning loop: every scan, look at leads that just reached a real outcome
# (a booked meeting, or a clear decline AFTER actually engaging - not just a
# dead address) and ask Gemini to distill ONE reusable writing rule from
# that real conversation, positive or negative. New rules land in
# lessons.txt and are automatically folded into every composer prompt from
# then on (see build_persona_text()) - a lightweight, prompt-level stand-in
# for "fine-tuning": no model weights change, but the persona effectively
# accumulates real-world lessons over time instead of staying static.
LESSONS_FILE = BASE_DIR / os.getenv("LESSONS_FILE", "lessons.txt")
LEARNING_ENABLED = os.getenv("LEARNING_ENABLED", "true").strip().lower() not in ("false", "0", "no")
LEARNING_BATCH_SIZE = int(os.getenv("LEARNING_BATCH_SIZE", "3"))

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
IMAP_HOST = "imap.gmail.com"

# Two column sets, one per table - used by load_leads()/update_lead()/
# add_lead_row() below to know which columns actually belong to which
# table's rows (and to filter out anything that doesn't, the same
# protection the old single COLS list gave every write).
COMPANY_COLS = ["company", "email", "status", "notes", "message_id", "in_reply_to",
                "draft_subject", "draft_body", "website", "company_research"]
PERSON_COLS = ["contact_name", "email", "company", "status", "notes", "message_id",
               "in_reply_to", "draft_subject", "draft_body", "job_title", "linkedin_info",
               "phone", "company_website", "company_research", "industry",
               "employee_count", "turnover", "city", "address",
               # Role-classification columns (see classify_role() near
               # compose_person_email()) - new; run the ALTER TABLE in
               # supabase_schema.sql before these actually persist. Every
               # write to them is wrapped in try/except (see _run_once())
               # so an un-migrated table never crashes a run - it just
               # can't remember the classification between runs yet.
               "role_category", "role_confidence", "role_method",
               # Persona label decided by classify_personas_batch() for this
               # row (or the literal string "NO_FITTING_ROLE") - see
               # add_matched_persona_column.sql. Every write to it is
               # wrapped in try/except same as the three columns above, so
               # an un-migrated table never crashes a run.
               "matched_persona",
               # Raw (UNSUMMARIZED) website scrape, filled in ahead of time
               # by prefetch_raw_scrapes() - see add_raw_scrape_column.sql
               # and the "prefetch-scrapes" CLI command. Lets a later `run`
               # skip the (sometimes slow) live website fetch entirely and
               # feed this straight into compose_person_emails_batch(),
               # which reads/summarizes it itself as part of writing the
               # email - no separate research_company() AI call needed for
               # a lead that already has one of these.
               "raw_scrape"]
TABLE_COLS = {SUPABASE_COMPANY_TABLE: COMPANY_COLS, SUPABASE_PERSON_TABLE: PERSON_COLS}
# Union of both, kept only for call sites that used to receive the old
# single "header" list as a catch-all (update_lead/add_lead_row below
# don't rely on this any more - they look the row's own table up in
# TABLE_COLS instead - but a couple of call sites still pass "header"
# through unused, and this keeps that harmless rather than removing it).
COLS = COMPANY_COLS + [c for c in PERSON_COLS if c not in COMPANY_COLS]
STATUSES_CONTACTABLE = {
    "contacted", "replied", "awaiting_slot", "awaiting_booking",
    # awaiting_referral: they declined for themselves but we asked if they
    # could point us to the right person, and are waiting on that reply.
    # needs_attention: the reply-handling agent flagged something it won't
    # touch on its own and emailed you directly - still watched here so a
    # follow-up message from the same lead isn't silently dropped, but
    # nothing auto-sends to them again until the flag is cleared by hand
    # (see _handle_reply's alert_needed check).
    "awaiting_referral", "needs_attention",
}

# Human labels for the fine-grained sub-steps reported via progress() below -
# used both by the dashboard's step-dots and printed straight to the
# terminal, so what's actually happening is visible even without the
# browser open (and gives you a plain, undeniable way to check you're
# running the code you think you're running: if these lines don't show up
# in the terminal, the process wasn't restarted with the latest file).
STEP_LABELS = {
    "connecting": "connecting to the AI",
    "loading": "loading leads from Supabase",
    "checking_new": "checking for new leads",
    "liveness": "checking email liveness",
    "website": "reading their website",
    "role_classify": "classifying their role",
    "compose": "writing the email",
    "compose_batch": "writing a batch of emails together",
    "reading": "reading their reply",
    "calendar": "checking the calendar",
    "sending": "sending",
    "referral": "writing referral email",
    "calendly_poll": "checking Calendly for new bookings",
    "learning": "learning from recent outcomes",
    "backfill_research": "catching up on company research",
    "backfill_role": "catching up on role classification",
}


def _report(progress, stage, current, total, label, step):
    """Reports a sub-step through the optional progress() callback (for the
    dashboard) AND prints it to the terminal (so it's visible either way)."""
    progress(stage, current, total, label, step=step)
    where = f" ({label})" if label else ""
    print(f"  ...{STEP_LABELS.get(step, step)}{where}")

# Guards Supabase leads against being read/written by the UI (ui_app.py) and a
# scheduled run at the same time.
LEADS_LOCK = threading.Lock()



# Every log_activity() call also gets appended here, in memory, for as long
# as the process runs - this is what lets the dashboard show a "here's what
# just happened" popup at the end of a scan (drafted, sent, booked, marked
# dead, etc.) without re-parsing activity_log.md or threading a new
# parameter through every call site that already reports through
# log_activity. cmd_run/the CLI never reads this, so it's a no-op there.
_ACTIVITY_EVENTS = []


def log_activity(kind, company, email_addr, subject="", body=""):
    """Append a human-readable entry to activity_log.md so you can see
    every draft, send, reply, and booking without digging through code."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [f"\n## {ts} — {kind} — {company or '(no company)'} <{email_addr}>"]
    if subject:
        lines.append(f"**Subject:** {subject}")
    if body:
        lines.append(f"\n{body}\n")
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    _ACTIVITY_EVENTS.append({
        "ts": ts, "kind": kind, "company": company or "", "email": email_addr or "",
        "subject": subject or "",
    })


NO_PERSONA_LOG_FILE = BASE_DIR / "no_persona_leads.md"


def log_no_persona_lead(company, contact_name, email_addr, job_title, reason):
    """Appends one line to no_persona_leads.md - a short, dedicated list of
    leads that need a human to pick (or add) a persona for, kept separate
    from the big activity_log.md. Deliberately NOT surfaced in the
    end-of-scan popup (see ui_app.py's showRunSummary(), which filters
    "ALERT" kinds out) so it stops interrupting you mid-scan - open the
    file (or the "Persona picks" link on the dashboard) whenever you
    actually want to work through it."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    who = contact_name or "(no name)"
    line = (f"- [{ts}] {company or '(no company)'} — {who} <{email_addr}> — "
            f"job title: {job_title or '(not given)'} — {reason}\n")
    with open(NO_PERSONA_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line)


def reset_activity_events():
    """Call before a scan starts, so get_activity_events() after it only
    returns what THIS scan did."""
    _ACTIVITY_EVENTS.clear()


def get_activity_events():
    """A snapshot list of every log_activity() call since the last
    reset_activity_events()."""
    return list(_ACTIVITY_EVENTS)


# --------------------------------------------------------------------------
# Leads storage - Supabase (Postgres). load_leads()/save_leads()/
# update_lead()/add_lead_row()/_find_lead_row_by_email() are the ENTIRE
# seam between "leads live somewhere" and everything else in this file -
# cmd_run, _handle_reply, _approve_pending, _reject_draft,
# _dismiss_attention, ui_app.py, etc. only ever call these five functions
# and read/write the plain dicts they hand back, so none of that code
# needed to change when the storage moved from leads.xlsx to Supabase.
# "_row" (a dict key on every lead returned by load_leads()) used to be an
# openpyxl spreadsheet row number - it's now that row's Supabase "id"
# instead, but every caller only ever passed it straight back into
# update_lead() without caring what it actually was, so nothing broke.
# --------------------------------------------------------------------------

_supabase_client = None


def _get_supabase():
    """Returns a cached Supabase client, creating it on first use. Exits
    with a clear message if the 'supabase' package isn't installed or
    SUPABASE_URL/SUPABASE_KEY aren't set - see SETUP.md's Supabase section."""
    global _supabase_client
    if _supabase_client is not None:
        return _supabase_client
    if _supabase_create_client is None:
        sys.exit("The 'supabase' package isn't installed - run:\n    pip install supabase")
    if not SUPABASE_URL or not SUPABASE_KEY:
        sys.exit(
            "SUPABASE_URL / SUPABASE_KEY aren't set in .env - get them from your "
            "Supabase project's Settings -> API (use the service_role key, not "
            "the anon key, so the bot can read/write freely), then add both to "
            ".env. See supabase_schema.sql for the table this expects."
        )
    _supabase_client = _supabase_create_client(SUPABASE_URL, SUPABASE_KEY)
    return _supabase_client


class _SupaSheet:
    """Drop-in stand-in for the openpyxl worksheet object this codebase
    used to pass around as "ws" - just enough surface (used only by
    update_lead()/add_lead_row()/_find_lead_row_by_email() below) that
    nothing else needs to know leads live in Postgres now.

    Now wraps BOTH tables (company_emails and personal_emails) instead of
    one. Every row's identity is a (table_name, id) tuple - not a bare id
    any more - so update_row()/insert_row() know which table to talk to
    without the caller having to track that separately. This is the one
    place that changed to make the rest of the file (update_lead,
    add_lead_row, check_inbox_for_replies, poll_calendly_bookings,
    run_learning_pass, cmd_approve/reject/dismiss_attention, ui_app.py...)
    keep working completely unchanged - they only ever passed row["_row"]
    back into update_lead() opaquely, never assumed what shape it was."""

    def __init__(self, client):
        self.client = client
        self.tables = {name: client.table(name) for name in TABLE_COLS}

    def update_row(self, row_id, updates):
        if not updates:
            return
        table_name, real_id = row_id
        self.tables[table_name].update(updates).eq("id", real_id).execute()

    def insert_row(self, table_name, fields):
        res = self.tables[table_name].insert(fields).execute()
        return (table_name, res.data[0]["id"])

    def find_by_email(self, email):
        """Searches BOTH tables for this address, company_emails first.
        Returns a (table_name, id) tuple, or None."""
        target = _clean_email(email)
        for table_name, tbl in self.tables.items():
            res = tbl.select("id, email").execute()
            for row in res.data or []:
                if row.get("email") and _clean_email(row["email"]) == target:
                    return (table_name, row["id"])
        return None


def load_leads():
    """Loads BOTH tables into one merged list of row dicts. Each row is
    tagged with "_table" (which table it came from - SUPABASE_COMPANY_TABLE
    or SUPABASE_PERSON_TABLE) and "_row" (a (table_name, id) tuple - what
    every update_lead()/add_lead_row() call uses to write back to the
    right place). Everything downstream (replies, Calendly polling,
    learning, approve/reject/dismiss, the dashboard) just iterates this one
    list and reads r.get(...) same as always - it never needed to know
    which table a row came from, and still doesn't, unless it wants to
    (see _run_once() below, which does)."""
    client = _get_supabase()
    ws = _SupaSheet(client)
    rows = []
    for table_name, cols in TABLE_COLS.items():
        res = ws.tables[table_name].select("*").order("id").execute()
        for row in res.data or []:
            values = {col: row.get(col) for col in cols}
            values["_row"] = (table_name, row["id"])
            values["_table"] = table_name
            rows.append(values)
    return client, ws, COLS, rows


def save_leads(wb):
    # Supabase writes happen immediately inside update_lead()/add_lead_row()
    # (each is its own API call) - there's nothing to flush at the end of a
    # batch of changes the way wb.save() used to write the whole workbook
    # at once. Kept as a no-op purely so every existing call site
    # (cmd_run, _handle_reply, ...) didn't need to change.
    pass


def update_lead(ws, header, row_idx, **updates):
    # row_idx is (table_name, id) - look up THAT table's real columns
    # rather than the generic union list, so a field that only exists on
    # the other table (e.g. accidentally passing job_title for a company
    # row) is silently dropped exactly like it always was, instead of
    # erroring out against Postgres.
    table_name = row_idx[0] if isinstance(row_idx, (tuple, list)) else None
    cols = TABLE_COLS.get(table_name, header)
    updates = {k: v for k, v in updates.items() if k in cols}
    ws.update_row(row_idx, updates)


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# --------------------------------------------------------------------------
# recipient classifier — company/role inbox vs a specific named person.
# This used to be one rule buried in a paragraph of icp.txt, which meant the
# model sometimes had to notice it itself. Making it real code means the
# same address is classified the same way every time, the decision is
# logged and visible (in the terminal and in the lead's Supabase notes), and the
# composer prompt gets told the answer directly instead of being asked to
# work it out.
# --------------------------------------------------------------------------

_GENERIC_INBOX_WORDS = {
    "info", "contact", "sales", "support", "service", "services", "office",
    "admin", "administrator", "hello", "hi", "help", "helpdesk", "team",
    "corporate", "hr", "marketing", "general", "enquiries", "inquiries",
    "mail", "reception", "frontdesk", "customerservice", "customercare",
    "webmaster", "noreply", "no-reply", "billing", "accounts", "media",
    "press", "careers", "jobs", "security", "operations", "ops",
}


def classify_recipient(email_addr):
    """Deterministic, no-API-call check: does this address look like a
    specific named person, or a shared/generic role inbox (info@, support@,
    office@...)? Returns {"kind": "generic_inbox" | "specific_person",
    "reason": str}. Best-effort by design - an address this doesn't
    recognize defaults to "specific_person" (the safer default: worst case
    you pitch someone who turns out to be a shared inbox, rather than
    under-pitching an actual decision-maker)."""
    local = (email_addr or "").split("@", 1)[0].strip().lower()
    normalized = re.sub(r"[._\-+0-9]+", "", local)
    if not normalized:
        return {"kind": "specific_person", "reason": "unparseable address - defaulting to person"}
    if normalized in _GENERIC_INBOX_WORDS:
        return {"kind": "generic_inbox",
                "reason": f"local part '{local}' matches a known role-inbox word ('{normalized}')"}
    # catches compounds like "salesteam@" / "infodesk@" / "hr-dept@" - only
    # as a prefix/suffix of the whole local part (not "contains anywhere"),
    # so a name that merely happens to contain a short generic word as a
    # substring in the middle - "chris@" containing "hr" - doesn't
    # false-positive. A short length cap keeps this to genuine compounds
    # rather than a long unrelated name that happens to start with one.
    for word in _GENERIC_INBOX_WORDS:
        if len(normalized) > len(word) + 5:
            continue
        if normalized.startswith(word) or normalized.endswith(word):
            return {"kind": "generic_inbox",
                    "reason": f"local part '{local}' looks like a role inbox ('{word}' + extra)"}
    return {"kind": "specific_person",
            "reason": f"local part '{local}' looks like a personal name/handle"}


# --------------------------------------------------------------------------
# banned-phrase guard — a second, code-level backstop (not just a prompt
# instruction) against the exact failure mode that slipped through once
# already: a reply that reads like an automated system message instead of a
# person (e.g. "removed you from the distribution list"). Checked against
# every templated email slot AND the not_interested closing reply before
# it's sent - if a banned pattern shows up anyway, the email is NOT sent
# silently; the caller falls back to a safe generic line and flags it in
# the terminal so it's visible that the model ignored an instruction.
# --------------------------------------------------------------------------

_BANNED_PHRASE_PATTERNS = [
    re.compile(r"distribution list", re.IGNORECASE),
    re.compile(r"mailing list", re.IGNORECASE),
    re.compile(r"unsubscribe", re.IGNORECASE),
    re.compile(r"removed you", re.IGNORECASE),
    re.compile(r"רשימת התפוצה"),
    re.compile(r"as an ai\b", re.IGNORECASE),
    re.compile(r"i hope this email finds you well", re.IGNORECASE),
    re.compile(r"i wanted to reach out regarding", re.IGNORECASE),
    re.compile(r"in today'?s fast-paced world", re.IGNORECASE),
    # Hebrew equivalents of the same robotic/mass-mail-sounding phrases,
    # now that EMAIL_LANGUAGE defaults to Hebrew.
    re.compile(r"מקווה שהמייל הזה מוצא אותך"),
    re.compile(r"להסיר (אותך|אתכם) מרשימת"),
    re.compile(r"הוסר(ת)? מרשימת התפוצה"),
    re.compile(r"בינה מלאכותית"),
    re.compile(r"בעולם המהיר של היום"),
]


def find_banned_phrase(*texts):
    """Returns the first banned pattern found across any of the given
    strings, or None if the text is clean. Cheap regex check, no API call -
    a last-resort backstop, not a substitute for good prompting."""
    for text in texts:
        if not text:
            continue
        for pattern in _BANNED_PHRASE_PATTERNS:
            m = pattern.search(text)
            if m:
                return m.group(0)
    return None


# --------------------------------------------------------------------------
# Style/structure lint for personal_emails' persona-composed first emails
# (see compose_person_email()) - a second, code-level backstop for the
# "STYLE RULES" section of FIRST_EMAIL_PERSONA_SYSTEM, same idea as
# find_banned_phrase() above: don't just trust the prompt, check the
# actual output before it's queued for approval.
#
#   - no dash characters anywhere in the sent text
#   - no rhetorical direct time-request phrasing ("15 minutes", "quick
#     call", etc.) - the template's own closing line is the only ask
#   - at most one concrete capability mentioned, from the site-type table
#     documented at the top of personal_email_templete.txt (more than one
#     match across different rows means the email is pitching two things
#     at once, which is exactly what the spec says never to do)
# --------------------------------------------------------------------------

_DASH_RE = re.compile(r"[-־]")

_RHETORICAL_TIME_ASK_RE = re.compile(
    r"(\bדקות\b|רבע שעה|שיחה של \d|\bminutes?\b|quick call|15 min)",
    re.IGNORECASE,
)

# The three fixed closing lines every persona email now has to end on
# (word for word - see _CLOSER_MARKER/find_closer_violation below). They're
# deliberately exempted from the dash/rhetorical-time-ask checks above -
# those two rules exist to stop the model from FREE-WRITING that kind of
# phrasing, but here it's the one thing we're actually asking it to copy
# verbatim, so it can't be allowed to trip the same alarm.
_CLOSER_VARIANTS = [
    "אשמח להראות ב-15 דקות איך זה נראה בול על המצלמות שלכם – כמה זמן חיפוש "
    "בהקלטות זה חוסך בפועל, ואיך נראית התראה בזמן אמת במקום לגלות אירוע רק "
    "למחרת. נוכל לתאם שיחה קצרה?",
    "יש לי דרך פשוטה להראות לצוות שלכם איך זה עובד בזמן אמת ובמה זה מחליף "
    "סריקה ידנית של שעות הקלטה בכל אירוע. שווה 15 דקות שיחה?",
    "אשמח להראות בשיחה קצרה בדיוק איך זה עובד על המקרה שלכם – מה זה חוסך "
    "בזמן טיפול באירועים, ואיך ההתראות בזמן אמת מונעות את הבעיה הבאה לפני "
    "שהיא קורה. מתאים שנקבע רבע שעה?",
]

# Each inner list is one "row" of the site-type -> capability table - a
# match against ANY phrase in a row counts as that ONE capability being
# mentioned; two DIFFERENT rows matching means two capabilities got into
# the same email.
_CAPABILITY_KEYWORD_GROUPS = [
    ["ציוד בטיחות", "קסדה", "ווסט", "helmet", "safety gear"],
    ["מחסן", "אדם לא מורשה", "מחוץ לשעות", "unauthorized", "after hours"],
    ["ונדליזם", "רכב חשוד", "vandalism", "suspicious vehicle"],
    ["נשק", "אזור מאובטח", "weapon", "secured area"],
]


def find_style_violation(subject, body):
    """Returns a short human-readable description of the first hard-rule
    violation found in this composed email, or None if it's clean. Checked
    on the FINAL assembled subject+body (fixed template text plus filled
    blanks together) - a violation could come from either side."""
    combined = f"{subject or ''}\n{body or ''}"

    # Strip out the one exact closer we DO require (see find_closer_violation)
    # before running the dash/rhetorical-time-ask checks below, so the
    # mandated closing line itself never trips the very rules it's exempt
    # from - those rules are about the model inventing that phrasing on its
    # own, not about the one fixed sentence we deliberately asked for.
    stripped = combined
    for variant in _CLOSER_VARIANTS:
        stripped = stripped.replace(variant, "")

    if _DASH_RE.search(stripped):
        return "contains a dash character (- or ־)"

    if _RHETORICAL_TIME_ASK_RE.search(stripped):
        return "asks for a specific amount of time / phrases the ask as a direct meeting request"

    hit_groups = [
        group for group in _CAPABILITY_KEYWORD_GROUPS
        if any(kw.lower() in combined.lower() for kw in group)
    ]
    if len(hit_groups) > 1:
        return "mentions more than one concrete capability from the site-type table"

    return None


def find_closer_violation(body):
    """personal_emails' persona emails must now end on ONE of the three
    fixed closing lines in _CLOSER_VARIANTS, copied word for word - unlike
    the optional "superhuman" personalization line, this one is mandatory
    (a persona email with no call-to-action at all is worse than one with a
    slightly-off personalization), so a mismatch here is always a
    violation to retry/flag, never something to silently blank out."""
    text = body or ""
    if any(variant in text for variant in _CLOSER_VARIANTS):
        return None
    return ("does not end on one of the three fixed closing lines, word for "
            "word - it must contain one of them exactly, not a paraphrase "
            "or a mix of two")


# --------------------------------------------------------------------------
# "Verbatim carrier" guard - both email_template.txt's opening line and
# every persona in personal_email_templete.txt give the model a FIXED
# sentence and tell it (in Hebrew): "write the following word for word,
# and replace ONLY the bracketed part(s)". That's what's supposed to keep
# these openers grounded in the real company_research instead of the model
# inventing its own claim about the company. In practice, when the
# research for a given lead is thin, the model sometimes ignores that
# instruction anyway and free-writes a DIFFERENT sentence altogether -
# which is exactly how a hallucinated, grammatically-off line like "אני
# יודע שבקרלטון יש צוות ביטחוני חזק... בלובי או בערוץ" gets in: the model
# guessed at specifics it was never actually given, in a sentence that
# doesn't match ANY of the templates' fixed carrier phrasing.
#
# Rather than trust the prompt alone, check for the fixed phrase actually
# being there. A blank whose instruction demands "word for word" is only
# ever compliant if it's either left empty (the templates' own designed
# fallback for "not enough real info") or the model's answer still
# contains every one of the fixed anchor phrases from ONE of the known
# carrier sentences below. Anything else gets discarded (treated as if the
# model had returned "") rather than sent - a missed personalization line
# is a much smaller problem than a made-up, ungrounded one.
#
# NB: if you add a new persona (or edit an existing one) that reuses this
# "word for word" pattern with a DIFFERENT fixed sentence, add its anchor
# phrase(s) here too, or this will end up discarding every answer for it.
# --------------------------------------------------------------------------

_VERBATIM_CARRIER_MARKER = "מילה במילה כלשונו"

_KNOWN_CARRIER_ANCHORS = [
    # personal_email_templete.txt - identical two-part carrier in all 4 personas
    ["קראתי עליכם קצת וראיתי שהלקוחות שלכם הם", "אני בלומנה פותר את הבעיה הזו בזה שאני"],
    # company_email_templete.txt - first "why you specifically" carrier
    ["עשיתי מחקר עליכם וראיתי שאתם מתעסקים ב"],
    # company_email_templete.txt - second "why this is relevant" carrier
    # (added alongside the fix for the referral-ask line coming back
    # grammatically broken - that free-written sentence is now a fixed
    # carrier too, same mechanism as the one above)
    ["וזה בדיוק הכיוון שרלוונטי אצלכם, בגלל"],
    # personal_email_templete.txt's mandatory closer blank - each of the
    # three fixed options counts as its own valid carrier (the model picks
    # ONE, not all three), so this check doesn't wipe a correct answer
    # before find_closer_violation() gets a chance to see it.
    *([variant] for variant in _CLOSER_VARIANTS),
]


def find_carrier_violation(instruction, filled_text):
    """None if this blank is fine; otherwise a short description of why the
    filled text was discarded. `instruction` is that ONE blank's own
    instruction text (not the whole email) - only blanks that actually ask
    for a fixed "word for word" sentence are checked at all."""
    text = (filled_text or "").strip()
    if not text:
        return None  # empty is the templates' own valid "not enough info" case
    if _VERBATIM_CARRIER_MARKER not in (instruction or ""):
        return None  # this blank was never a fixed-carrier one
    for anchors in _KNOWN_CARRIER_ANCHORS:
        if all(a in text for a in anchors):
            return None
    return ("ignored the \"word for word\" instruction and wrote a different, "
            "likely-invented sentence instead of the fixed carrier phrase")


def add_lead_row(ws, header, table, **fields):
    """Insert a brand-new row into the given table (SUPABASE_COMPANY_TABLE
    or SUPABASE_PERSON_TABLE) - mirrors update_lead's style, but for a row
    that doesn't exist yet. Returns the new row's (table_name, id)."""
    cols = TABLE_COLS.get(table, header)
    fields = {k: v for k, v in fields.items() if k in cols}
    return ws.insert_row(table, fields)


def _find_lead_row_by_email(ws, header, email):
    """Return the (table_name, id) of an existing row with this email
    (checked across BOTH tables, cleaned comparison), or None if there
    isn't one yet."""
    return ws.find_by_email(email)


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"last_imap_uid": 0}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# --------------------------------------------------------------------------
# liveness check (SMTP RCPT TO probe, no message actually sent)
# --------------------------------------------------------------------------

def _lookup_mx(domain):
    """Returns (mx_host_or_reason, verdict) where verdict is:
      "ok"      - found a mail server to probe
      "dead"    - DNS clearly says there's no mail for this domain
                  (domain doesn't exist, or no MX and no A record)
      "unknown" - DNS didn't answer at all (timeout / resolver down) -
                  NOT evidence the address is bad.
    Tries your normal resolver first, then public ones (Google, Cloudflare),
    since a slow home router is the usual cause of timeouts."""
    try:
        import dns.resolver
        import dns.exception
    except ImportError:
        return "dnspython not installed", "unknown"

    resolvers = []
    try:
        system = dns.resolver.Resolver()
        system.lifetime = 8
        resolvers.append(system)
    except Exception:
        pass
    public = dns.resolver.Resolver(configure=False)
    public.nameservers = ["8.8.8.8", "1.1.1.1"]
    public.lifetime = 8
    resolvers.append(public)

    last_error = "no resolver answered"
    for res in resolvers:
        try:
            answers = res.resolve(domain, "MX")
            mx = str(sorted(answers, key=lambda r: r.preference)[0].exchange).rstrip(".")
            return mx, "ok"
        except dns.resolver.NXDOMAIN:
            return f"domain doesn't exist ({domain})", "dead"
        except dns.resolver.NoAnswer:
            # No MX record - by the mail standard, servers fall back to the
            # domain's plain A record, so only dead if that's missing too.
            try:
                res.resolve(domain, "A")
                return domain, "ok"
            except dns.resolver.NoAnswer:
                return f"no MX or A record for {domain}", "dead"
            except Exception as e:
                last_error = str(e)
                continue
        except (dns.exception.Timeout, dns.resolver.NoNameservers) as e:
            last_error = str(e)
            continue  # try the next resolver
        except Exception as e:
            last_error = str(e)
            continue
    return f"DNS didn't answer: {last_error}", "unknown"


def _clearout_verify(addr):
    """Calls Clearout's instant single-email verification API for one
    address. Returns (safe_to_send, status) - both lowercased strings
    straight from Clearout's response - on a normal API reply, or None if
    the call itself couldn't be completed at all (not configured, network
    error, bad key, quota exhausted, malformed response). None is the
    signal for the caller to fall back to the built-in MX/SMTP check
    instead - a Clearout hiccup should never be treated as "this lead is
    dead", same philosophy as every other network-dependent check in this
    file."""
    if not CLEAROUT_API_KEY:
        return None
    import requests
    try:
        resp = requests.post(
            CLEAROUT_VERIFY_URL,
            headers={
                "Authorization": f"Bearer {CLEAROUT_API_KEY}",
                "Content-Type": "application/json",
            },
            json={"email": addr, "timeout": CLEAROUT_TIMEOUT_MS},
            timeout=(CLEAROUT_TIMEOUT_MS / 1000) + 10,
        )
    except requests.RequestException as e:
        print(f"  Clearout API call failed for {addr}: {e} - falling back "
              f"to the built-in MX/SMTP check instead.")
        return None

    if resp.status_code != 200:
        print(f"  Clearout API returned HTTP {resp.status_code} for {addr} "
              f"- falling back to the built-in MX/SMTP check instead.")
        return None

    try:
        payload = resp.json()
    except ValueError:
        print(f"  Clearout API returned a non-JSON response for {addr} - "
              f"falling back to the built-in MX/SMTP check instead.")
        return None

    if payload.get("error"):
        err = payload["error"]
        print(f"  Clearout API error for {addr}: "
              f"{err.get('message', err) if isinstance(err, dict) else err} "
              f"- falling back to the built-in MX/SMTP check instead.")
        return None

    data = payload.get("data") or {}
    safe_to_send = (data.get("safe_to_send") or "").strip().lower()
    status = (data.get("status") or "").strip().lower()
    return safe_to_send, status


def is_probably_live(addr, timeout=8):
    """Best-effort check: is this address actually worth drafting/sending
    to? Tries Clearout's real email-verification API first (see
    _clearout_verify() above and CLEAROUT_API_KEY near the top of this
    file) - only when that's unset or the call itself fails does this fall
    back to the original MX/SMTP probe below. Many big providers (Gmail,
    Outlook) always say "yes" at the SMTP-probe stage (they check the
    address at delivery time instead), so that fallback mainly catches
    typo'd domains and clearly dead addresses - it's never 100% reliable,
    which is exactly why Clearout is preferred whenever it's configured."""
    if SKIP_LIVENESS_CHECK:
        return True, "liveness check skipped (SKIP_LIVENESS_CHECK=true) - assuming live"
    clearout = _clearout_verify(addr)
    if clearout is not None:
        safe_to_send, status = clearout
        if safe_to_send == "no":
            return False, f"Clearout: not safe to send (status: {status or 'unknown'})"
        if safe_to_send == "yes":
            return True, f"Clearout: verified ({status or 'valid'})"
        # "risky", "unknown", or anything else Clearout wasn't confident
        # about - don't guess dead off an uncertain verdict, same rule as
        # every other "couldn't fully verify" case in this function.
        return True, f"Clearout: uncertain ({safe_to_send or status or 'no verdict'}) - assuming live"

    try:
        domain = addr.split("@", 1)[1]
    except IndexError:
        return False, "malformed address"

    mx_host, verdict = _lookup_mx(domain)
    if verdict == "dead":
        return False, mx_host  # mx_host carries the reason string here
    if verdict == "unknown":
        # DNS itself didn't answer (timeout, local resolver down) - that says
        # nothing about the lead. Never mark someone dead over our own
        # network hiccup; same philosophy as the SMTP-probe fallback below.
        return True, f"couldn't verify, assuming live ({mx_host})"

    try:
        server = smtplib.SMTP(mx_host, 25, timeout=timeout)
        server.helo("verify.local")
        server.mail("verify@verify.local")
        code, _ = server.rcpt(addr)
        server.quit()
        if code in (250, 251):
            return True, "accepted"
        if code in (450, 451, 452):
            return True, "greylisted (treat as live)"
        return False, f"rejected ({code})"
    except (socket.timeout, smtplib.SMTPException, OSError) as e:
        # Most residential/cloud IPs get blocked on port 25 outbound —
        # if we can't probe, don't block sending, just say "unknown".
        return True, f"probe failed, assuming live ({e})"


def cmd_prefetch_scrapes(args):
    """`python outreach_bot.py prefetch-scrapes --count N` - fills
    personal_emails.raw_scrape for up to N leads ahead of time (see
    prefetch_raw_scrapes()'s docstring for why this is its own command
    instead of happening inside `run`). Meant to be put on its own
    schedule, separate from `run`. Doesn't wrap the call in LEADS_LOCK
    itself any more - prefetch_raw_scrapes() takes the lock only around
    its own reads/writes (LEADS_LOCK isn't reentrant, so double-wrapping
    it here would deadlock)."""
    filled = prefetch_raw_scrapes(int(args.count))
    print(f"Prefetched raw website text for {filled} personal_emails lead(s).")


def run_prefetch_after_run():
    """Prefetches website text for a handful of personal_emails leads that
    don't have raw_scrape yet - meant to be called right AFTER a `run` has
    already reported/printed that it's done (see cmd_run() below and
    ui_app.py's do_scan(), which calls this only after updating
    scheduler_state to reflect the finished run), NOT from inside
    _run_once() itself - so prefetching for some FUTURE run never delays
    the notice that THIS run just finished. Does NOT hold LEADS_LOCK itself
    any more - prefetch_raw_scrapes() takes it only around its own reads/
    writes, releasing it for the slow network fetches in between, so the
    dashboard's /api/status can keep getting a fresh, non-stale snapshot
    throughout the whole prefetch instead of only after it finishes. Set
    PREFETCH_SCRAPE_COUNT=0 in .env to turn this off entirely. Never
    raises."""
    count = int(os.getenv("PREFETCH_SCRAPE_COUNT", "20"))
    if count <= 0:
        return
    try:
        prefetched = prefetch_raw_scrapes(count)
        if prefetched:
            print(f"Prefetched raw website text for {prefetched} personal_emails "
                  f"lead(s), ready for a future run.")
    except Exception as e:
        print(f"  end-of-run scrape prefetch failed: {type(e).__name__}: {e} - "
              f"skipping it this run, next run will try again.")


# --------------------------------------------------------------------------
# pull: figure out what needs a drafted email
# --------------------------------------------------------------------------

def cmd_pull(args):
    wb, ws, header, rows = load_leads()
    state = load_state()

    to_contact = []
    new_rows = [r for r in rows if (r.get("status") or "new").strip().lower() == "new"]
    for r in new_rows[:BATCH_SIZE]:
        live, reason = is_probably_live(r["email"])
        if live:
            to_contact.append({"row": r["_row"], "company": r["company"], "email": r["email"]})
        else:
            update_lead(ws, header, r["_row"], status="dead", notes=f"liveness check: {reason}")
    save_leads(wb)

    to_reply = check_inbox_for_replies(rows, state)
    save_state(state)

    pending = {"to_contact": to_contact, "to_reply": to_reply}
    PENDING_FILE.write_text(json.dumps(pending, indent=2, ensure_ascii=False))
    print(json.dumps(pending, indent=2, ensure_ascii=False))


def _decode(s):
    if not s:
        return ""
    parts = decode_header(s)
    out = []
    for text, enc in parts:
        if isinstance(text, bytes):
            out.append(text.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(text)
    return "".join(out)


# Matches the header line mail clients insert above a quoted original
# message - "On Wed, Sep 16, 2026 at 10:47 PM <x@y.com> wrote:" (Gmail/Apple
# Mail, English), the Hebrew Gmail equivalent ("בתאריך ... כתב/ה:"), and
# Outlook-style headers. Whatever comes at/after this in a reply is the
# ORIGINAL email being quoted back, not something the lead wrote.
_QUOTE_HEADER_PATTERNS = [
    re.compile(r"^\s*On .{0,120} wrote:\s*$", re.MULTILINE),
    # Hebrew Gmail's quote header always starts the line with "בתאריך" and
    # ends in a colon - the middle varies by version ("... מאת X:",
    # "... מאת X כתב:", "... כתבה:"), so match on the stable start/end
    # rather than the exact wording in between.
    re.compile(r"^\s*בתאריך .{0,160}:\s*$", re.MULTILINE),
    re.compile(r"^\s*-{2,}\s*Original Message\s*-{2,}", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^\s*From:\s.+\n\s*Sent:\s.+\n\s*To:\s.+\n\s*Subject:\s.+", re.MULTILINE),
]


def _strip_quoted_reply(text):
    """Cuts off the quoted original message that Gmail/Outlook/etc. glue
    onto the bottom of a reply, so only what the lead actually typed gets
    sent to the model. Without this, "their message" includes our entire
    original cold-pitch email quoted back - which is exactly what makes the
    bot look like it's re-pitching out of nowhere or missing a proposed
    time buried underneath a wall of quoted text."""
    if not text:
        return text
    cut_at = len(text)
    for pattern in _QUOTE_HEADER_PATTERNS:
        m = pattern.search(text)
        if m and m.start() < cut_at:
            cut_at = m.start()
    trimmed = text[:cut_at].rstrip()

    # Some clients quote without a header line at all - just a run of
    # "> "-prefixed lines. Trim a trailing run of those too.
    lines = trimmed.splitlines()
    while lines and lines[-1].strip().startswith(">"):
        lines.pop()
    trimmed = "\n".join(lines).strip()

    # Safety net: if stripping somehow ate the whole message (e.g. the
    # lead's own text happened to start with a matching line), fall back
    # to the original rather than sending Gemini an empty reply.
    return trimmed if trimmed else text.strip()


def _clean_email(raw):
    """Normalize an email address for comparison: lowercase, trim ordinary
    whitespace, and strip invisible Unicode formatting characters (RTL/LTR
    marks, zero-width joiners, etc.) that can sneak into an Excel cell when
    copy-pasting mixed Hebrew/English text - those are invisible so they
    look like a perfect match in the spreadsheet but silently break a
    plain string comparison against the address parsed from an email
    header."""
    if not raw:
        return ""
    cleaned = "".join(ch for ch in str(raw) if unicodedata.category(ch) != "Cf")
    return cleaned.strip().lower()


def _parse_bulk_from_headers(fetch_response):
    """imaplib's response to a multi-UID header-only FETCH is a flat list
    mixing (metadata, header-bytes) tuples with plain closing-paren bytes
    per message. Pulls out {uid_str: from_address} from that. Used to
    cheaply filter thousands of messages down to the handful worth
    downloading in full, instead of fetching every message's entire body
    just to check who it's from."""
    result = {}
    for item in fetch_response:
        if not isinstance(item, tuple) or len(item) < 2:
            continue
        meta, header_bytes = item[0], item[1]
        m = re.search(rb"UID (\d+)", meta)
        if not m or not header_bytes:
            continue
        uid_str = m.group(1).decode()
        try:
            header_text = header_bytes.decode("utf-8", errors="replace")
        except Exception:
            continue
        from_line = ""
        for line in header_text.splitlines():
            if line.lower().startswith("from:"):
                from_line = line[5:].strip()
                break
        _, addr = parseaddr(from_line)
        result[uid_str] = _clean_email(addr)
    return result


def check_inbox_for_replies(rows, state):
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        print("Inbox check: skipped - GMAIL_USER/GMAIL_APP_PASSWORD not set in .env.")
        return []

    self_addr = _clean_email(GMAIL_USER)
    lead_by_email = {
        _clean_email(r["email"]): r
        for r in rows
        if (r.get("status") or "").strip().lower() in STATUSES_CONTACTABLE
        # Never treat the bot's own sending address as a "lead who might
        # reply" - see the matching guard in _run_once(): a self-addressed
        # email lands in the bot's own inbox and would otherwise look like
        # an incoming reply from itself.
        and _clean_email(r["email"]) != self_addr
    }
    if not lead_by_email:
        print(f"Inbox check: skipped - no leads currently in "
              f"{sorted(STATUSES_CONTACTABLE)} (nothing to watch replies for right now).")
        return []

    replies = []
    imap = imaplib.IMAP4_SSL(IMAP_HOST)
    imap.login(GMAIL_USER, GMAIL_APP_PASSWORD)
    imap.select("INBOX")

    last_uid = state.get("last_imap_uid", 0)
    status, data = imap.uid("search", None, f'(UID {last_uid + 1}:*)')
    raw_uids = data[0].split() if data and data[0] else []
    # Defensive filter: Gmail's IMAP search for "UID next:*" can sometimes
    # hand back UIDs at or below what we asked for (a known quirk when
    # there's no mail above that UID yet) instead of an empty result. Never
    # trust the server's range to be honored - drop anything that isn't
    # actually newer than our bookmark before we do anything else with it.
    uids = [u for u in raw_uids if int(u) > last_uid]
    stale = len(raw_uids) - len(uids)
    print(f"Inbox check: {len(uids)} new message(s) in INBOX since UID {last_uid} "
          f"(watching for replies from {len(lead_by_email)} lead(s) currently "
          f"in {sorted(STATUSES_CONTACTABLE)})."
          + (f" [ignored {stale} stale UID(s) <= bookmark that Gmail's search "
             f"returned anyway]" if stale else ""))

    if not uids:
        state["last_imap_uid"] = last_uid
        imap.logout()
        return []

    # Cheap pass first: pull just the From header for every new message in
    # ONE round trip (not one fetch per message - with a big/old mailbox
    # this can be thousands), then only download the full body for whatever
    # actually matches a contactable lead. On a fresh mailbox with no
    # bookmark yet this can legitimately be years of newsletters/receipts;
    # this keeps that first scan fast instead of grinding through them.
    max_uid_seen = last_uid
    from_by_uid = {}
    uid_set = b",".join(uids)
    try:
        hstatus, header_resp = imap.uid("fetch", uid_set, "(BODY.PEEK[HEADER.FIELDS (FROM)])")
        if hstatus == "OK" and header_resp:
            from_by_uid = _parse_bulk_from_headers(header_resp)
    except imaplib.IMAP4.error as e:
        print(f"  bulk header fetch failed ({e}), falling back to per-message checks")

    matched_uids = []
    for uid in uids:
        uid_int = int(uid)
        max_uid_seen = max(max_uid_seen, uid_int)
        uid_str = uid.decode()
        from_addr = from_by_uid.get(uid_str)
        if from_addr is None:
            # bulk fetch didn't give us this one (older server, odd
            # response) - fall back to checking it the slow way below
            matched_uids.append(uid)
        elif from_addr in lead_by_email:
            matched_uids.append(uid)

    skipped = len(uids) - len(matched_uids)
    if skipped:
        print(f"  {skipped} of them aren't from a currently-contactable lead "
              f"(not a known lead in Supabase, address doesn't match exactly, or that "
              f"lead's status already moved past {sorted(STATUSES_CONTACTABLE)}) - skipping those.")
        # Show exactly who they were from, in plain terms - when it's just a
        # handful, this is the whole diagnosis: compare the address printed
        # here, character for character, against the email cell in
        # Supabase (a copy-pasted address can carry an invisible
        # character that looks identical but doesn't match).
        if skipped <= 10:
            for uid in uids:
                uid_str = uid.decode()
                if uid_str not in {u.decode() for u in matched_uids}:
                    seen_from = from_by_uid.get(uid_str, "(unknown - not in bulk header fetch)")
                    print(f"    UID {uid_str} was from: {seen_from!r}")
            print(f"    currently-contactable addresses on file: "
                  f"{sorted(lead_by_email.keys())!r}")

    # Save the watermark now, right after the cheap scan, rather than only
    # at the very end - so if something goes wrong during the slower
    # full-body fetch/compose step below, a re-run doesn't have to walk
    # through all these thousands of messages again from scratch.
    state["last_imap_uid"] = max_uid_seen
    save_state(state)

    for uid in matched_uids:
        status, msg_data = imap.uid("fetch", uid, "(RFC822)")
        if status != "OK" or not msg_data or not msg_data[0]:
            print(f"  (skip) UID {uid}: couldn't fetch it ({status})")
            continue
        msg = email.message_from_bytes(msg_data[0][1])
        _, from_addr = parseaddr(msg.get("From", ""))
        from_addr = _clean_email(from_addr)
        if from_addr not in lead_by_email:
            continue
        print(f"  (match) UID {uid}: reply from {from_addr} ({lead_by_email[from_addr]['company']})")

        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    body += part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", errors="replace"
                    )
        else:
            body = msg.get_payload(decode=True).decode(
                msg.get_content_charset() or "utf-8", errors="replace"
            )

        lead = lead_by_email[from_addr]
        subject = _decode(msg.get("Subject", ""))
        message = _strip_quoted_reply(body.strip())[:3000]
        sent_at = None
        try:
            from email.utils import parsedate_to_datetime
            parsed = parsedate_to_datetime(msg.get("Date", ""))
            if parsed is not None:
                sent_at = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except Exception:
            sent_at = None
        replies.append({
            "row": lead["_row"],
            "_table": lead.get("_table"),
            "company": lead["company"],
            "email": from_addr,
            "subject": subject,
            "message": message,
            "message_id": msg.get("Message-Id", ""),
            "sent_at": sent_at,
        })

        # Stash the reply itself (not just that one happened) in state.json,
        # keyed by lead email, so the dashboard can show an actual inbox of
        # what people wrote back - not just a status pill.
        lead_state = state.setdefault("leads", {}).setdefault(from_addr, {})
        lead_state["last_reply_subject"] = subject
        lead_state["last_reply_message"] = message
        lead_state["last_reply_at"] = datetime.now().isoformat()

    imap.logout()
    return replies


# --------------------------------------------------------------------------
# send: actually deliver drafted emails
# --------------------------------------------------------------------------

_HEBREW_RE = re.compile(r"[֐-׿]")


def _is_hebrew_text(text):
    """True if the text contains any Hebrew characters - used to decide
    whether an email should render right-to-left. Plain-text emails leave
    this up to each mail client's own (inconsistent) auto-detection, which
    is why Hebrew bodies were showing up left-aligned in Gmail - sending an
    explicit RTL HTML version fixes that everywhere."""
    return bool(_HEBREW_RE.search(text or ""))


def _body_to_rtl_html(body):
    """Turns a plain-text email body into a minimal HTML version that's
    forced right-to-left and right-aligned (dir="rtl" at both the <html>
    and inner <div> level - some clients, Gmail included, only honor one
    or the other depending on context). Paragraphs (blank-line-separated)
    become <p> tags, single newlines become <br>, and bare URLs become
    actual clickable <a> links - the Calendly link specifically gets a
    friendly Hebrew label ("לקביעת פגישה") instead of showing the long raw
    URL as the link text; any other URL falls back to showing itself as
    the link text (still clickable, just no nice label available)."""
    import html as _html

    url_re = re.compile(r"(https?://\S+)")

    def linkify_escaped(m):
        # m is matched against already-HTML-escaped text, so the URL here
        # may contain entities (e.g. a literal '&' in a query string shows
        # up as '&amp;'). Unescape it back to the real URL for the href and
        # for comparing against CALENDLY_LINK, then strip common trailing
        # punctuation (a period/comma ending the sentence) so the link
        # itself doesn't 404, same as most email clients' own auto-linking.
        url = _html.unescape(m.group(1))
        trailing = ""
        while url and url[-1] in ".,;:!?)":
            trailing = url[-1] + trailing
            url = url[:-1]
        label = "לקביעת פגישה" if CALENDLY_LINK and url == CALENDLY_LINK else url
        return f'<a href="{_html.escape(url)}">{_html.escape(label)}</a>{_html.escape(trailing)}'

    paragraphs = re.split(r"\n\s*\n", body.strip())
    html_paragraphs = []
    for para in paragraphs:
        escaped = _html.escape(para).replace("\n", "<br>\n")
        escaped = url_re.sub(linkify_escaped, escaped)
        html_paragraphs.append(f"<p>{escaped}</p>")
    body_html = "\n".join(html_paragraphs)
    return (
        '<html dir="rtl"><body dir="rtl" '
        'style="direction: rtl; text-align: right; font-family: Arial, sans-serif; '
        'font-size: 14px;">\n'
        f'<div dir="rtl" style="direction: rtl; text-align: right;">\n{body_html}\n</div>\n'
        "</body></html>"
    )


def send_one(to_addr, subject, body, in_reply_to=None):
    if _is_hebrew_text(body):
        # Multipart so clients that can't/won't render HTML still get a
        # sane plain-text fallback; the HTML part is what makes Gmail show
        # it right-aligned like a normal Hebrew email instead of jammed to
        # the left margin.
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(body, "plain", "utf-8"))
        msg.attach(MIMEText(_body_to_rtl_html(body), "html", "utf-8"))
    else:
        msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = GMAIL_USER
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg_id = make_msgid()
    msg["Message-Id"] = msg_id
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
        server.starttls()
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_USER, [to_addr], msg.as_string())
    return msg_id


def send_alert_email(company, lead_email, reason, their_message):
    """Sends YOU (GMAIL_USER) a real email the moment a reply gets flagged
    with alert_needed=true - the reasoning being that a dashboard flag alone
    is only seen if/when you happen to open the dashboard, but an email
    lands wherever you already check. Best-effort: if this fails (bad
    GMAIL_USER/app password, no network), it's logged and swallowed rather
    than raised - a notification failing should never be what takes down
    the rest of a scan, and the lead is still marked needs_attention in
    Supabase either way so it isn't lost."""
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        print("  (couldn't send the alert email - GMAIL_USER/GMAIL_APP_PASSWORD not set)")
        return
    subject = f"[Outreach bot] Needs your attention — {company or lead_email}"
    body = (
        f"A reply from {company or '(no company)'} <{lead_email}> was flagged for "
        f"your attention instead of being handled automatically.\n\n"
        f"Why: {reason}\n\n"
        f"Their message:\n\"\"\"\n{their_message}\n\"\"\"\n\n"
        f"No reply was sent to them - this lead is marked needs_attention in "
        f"Supabase until you handle it (change its status once you have, so "
        f"the bot watches it normally again)."
    )
    try:
        send_one(GMAIL_USER, subject, body)
    except Exception as e:
        print(f"  couldn't send the alert email for {lead_email}: {e}")


def cmd_send(args):
    drafts = json.loads(Path(args.file).read_text())
    wb, ws, header, rows = load_leads()
    rows_by_num = {r["_row"]: r for r in rows}

    for d in drafts:
        row = rows_by_num.get(d["row"])
        in_reply_to = row.get("message_id") if row else None
        msg_id = send_one(d["email"], d["subject"], d["body"], in_reply_to=in_reply_to)
        update_lead(
            ws, header, d["row"],
            status=d.get("new_status", "contacted"),
            message_id=msg_id,
            notes=d.get("notes", ""),
        )
        print(f"sent to {d['email']} -> status={d.get('new_status', 'contacted')}")
        time.sleep(2)  # small gap between sends in the same batch

    save_leads(wb)


# --------------------------------------------------------------------------
# calendar (Google Calendar API)
# --------------------------------------------------------------------------

class CalendarNotConfigured(RuntimeError):
    """Calendar auth is missing/broken. Deliberately NOT a sys.exit() - this
    can be raised deep inside the per-lead reply loop in _run_once, and one
    lead's calendar problem should never be allowed to kill the entire run
    (and silently drop every other lead's progress with it). CLI commands
    that expect to run standalone (slots/book) catch this and turn it into
    a clean sys.exit themselves."""
    pass


def get_calendar_service():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    SCOPES = ["https://www.googleapis.com/auth/calendar"]
    token_path = BASE_DIR / "token.json"
    if not token_path.exists():
        raise CalendarNotConfigured(
            "No token.json found. Run the one-time authorization first: "
            "python authorize_calendar.py step1 "
            "(this bot runs unattended, so it can't pop open a browser itself)"
        )
    creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            token_path.write_text(creds.to_json())
        else:
            raise CalendarNotConfigured(
                "Saved calendar token is invalid and can't be refreshed. "
                "Re-run: python authorize_calendar.py step1"
            )
    return build("calendar", "v3", credentials=creds)


def _find_open_slots(service, count):
    # Timezone-aware throughout - freebusy gives back aware datetimes, and
    # comparing those against a naive datetime.utcnow() raises TypeError.
    now = datetime.now(timezone.utc)
    end = now + timedelta(days=int(os.getenv("SLOT_SEARCH_DAYS", "7")))

    body = {
        "timeMin": now.isoformat(),
        "timeMax": end.isoformat(),
        "timeZone": "UTC",
        "items": [{"id": "primary"}],
    }
    fb = service.freebusy().query(body=body).execute()
    busy = fb["calendars"]["primary"]["busy"]

    work_start_h = int(os.getenv("WORK_START_HOUR", "9"))
    work_end_h = int(os.getenv("WORK_END_HOUR", "18"))
    slot_len = timedelta(minutes=30)

    def overlaps(a_start, a_end):
        for b in busy:
            b_start = datetime.fromisoformat(b["start"].replace("Z", "+00:00"))
            b_end = datetime.fromisoformat(b["end"].replace("Z", "+00:00"))
            if a_start < b_end and a_end > b_start:
                return True
        return False

    slots = []
    # WORK_START_HOUR/WORK_END_HOUR are local business hours (e.g. 9-18
    # Asia/Jerusalem), not UTC ones - walk forward in real (UTC) time but
    # judge weekday/work-hour against the local wall clock, same as
    # _slot_is_free does for a lead's own proposed time.
    cursor = now + timedelta(hours=2)
    cursor = cursor.astimezone(_local_tz()).replace(
        minute=0, second=0, microsecond=0
    ).astimezone(timezone.utc)
    while len(slots) < int(count) and cursor < end:
        local_cursor = cursor.astimezone(_local_tz())
        if local_cursor.weekday() in _work_days() and work_start_h <= local_cursor.hour < work_end_h:
            slot_end = cursor + slot_len
            if not overlaps(cursor, slot_end):
                slots.append({"start": cursor.isoformat(), "end": slot_end.isoformat()})
        cursor += slot_len
    return slots


def _local_tz():
    return ZoneInfo(os.getenv("TIMEZONE", "Asia/Jerusalem"))


def _work_days():
    """Which weekdays count as bookable, as Python's weekday() numbers
    (Monday=0 ... Sunday=6). Defaults to Israel's actual work week
    (Sunday-Thursday) rather than the Monday-Friday week that "weekday() <
    5" silently assumes - that Western default was wrongly rejecting
    Sunday meetings and accepting Friday ones for a business running on
    Asia/Jerusalem time. Override with WORK_DAYS in .env as comma-separated
    numbers, e.g. WORK_DAYS=6,0,1,2,3 (this same Sun-Thu default) or
    WORK_DAYS=0,1,2,3,4 for a Monday-Friday week."""
    raw = os.getenv("WORK_DAYS", "").strip()
    if raw:
        try:
            return {int(x) for x in raw.split(",") if x.strip() != ""}
        except ValueError:
            pass
    return {6, 0, 1, 2, 3}  # Sunday, Monday, Tuesday, Wednesday, Thursday


def _fmt_local(iso_str):
    """Render a stored slot's ISO datetime string in local wall-clock time
    for a prompt/log, instead of a raw UTC-offset ISO string - both for
    Gemini's benefit (it should talk about times the way a person would)
    and for anyone reading activity_log.md."""
    try:
        dt = datetime.fromisoformat(iso_str)
        return dt.astimezone(_local_tz()).strftime("%A, %Y-%m-%d %H:%M")
    except Exception:
        return iso_str


def _parse_local_datetime(s):
    """Turns the composer's "YYYY-MM-DD HH:MM" (local time) string - the
    format it's told to use for a time a LEAD proposed themselves, e.g.
    "free tomorrow at 4?" - into an aware datetime. Returns None if it's
    missing, malformed, or in the past; this is model output, not
    something we control the shape of, so stay defensive and just fall
    back to the normal "offer real slots" flow rather than raising."""
    if not s or not isinstance(s, str):
        return None
    try:
        naive = datetime.strptime(s.strip(), "%Y-%m-%d %H:%M")
    except ValueError:
        return None
    dt = naive.replace(tzinfo=_local_tz())
    if dt < datetime.now(_local_tz()) - timedelta(minutes=5):
        return None
    return dt


def _slot_is_free(service, start_dt, end_dt):
    """Checks one specific time against the calendar (and basic work-hours
    sanity) rather than the batch of pre-generated slots - used when a lead
    proposes their own time instead of picking from what we offered."""
    work_start_h = int(os.getenv("WORK_START_HOUR", "9"))
    work_end_h = int(os.getenv("WORK_END_HOUR", "18"))
    local_start = start_dt.astimezone(_local_tz())
    if local_start.weekday() not in _work_days() or not (work_start_h <= local_start.hour < work_end_h):
        return False

    start_utc = start_dt.astimezone(timezone.utc)
    end_utc = end_dt.astimezone(timezone.utc)
    body = {
        "timeMin": start_utc.isoformat().replace("+00:00", "Z"),
        "timeMax": end_utc.isoformat().replace("+00:00", "Z"),
        "timeZone": "UTC",
        "items": [{"id": "primary"}],
    }
    fb = service.freebusy().query(body=body).execute()
    busy = fb["calendars"]["primary"]["busy"]
    for b in busy:
        b_start = datetime.fromisoformat(b["start"].replace("Z", "+00:00"))
        b_end = datetime.fromisoformat(b["end"].replace("Z", "+00:00"))
        if start_utc < b_end and end_utc > b_start:
            return False
    return True


def cmd_slots(args):
    try:
        service = get_calendar_service()
    except CalendarNotConfigured as e:
        sys.exit(str(e))
    slots = _find_open_slots(service, int(args.count))
    print(json.dumps(slots, indent=2))


def cmd_book(args):
    try:
        service = get_calendar_service()
    except CalendarNotConfigured as e:
        sys.exit(str(e))
    event = {
        "summary": args.subject or f"Intro call with {args.company}",
        "description": f"Booked automatically by the outreach bot with {args.company}.",
        "start": {"dateTime": args.start, "timeZone": os.getenv("TIMEZONE", "Asia/Jerusalem")},
        "end": {"dateTime": args.end, "timeZone": os.getenv("TIMEZONE", "Asia/Jerusalem")},
        "attendees": [{"email": args.email}],
    }
    created = service.events().insert(
        calendarId="primary", body=event, sendUpdates="all"
    ).execute()

    wb, ws, header, rows = load_leads()
    for r in rows:
        if (r["email"] or "").strip().lower() == args.email.strip().lower():
            update_lead(ws, header, r["_row"], status="booked",
                        notes=f"meeting booked: {args.start}")
    save_leads(wb)

    print(json.dumps({"eventId": created.get("id"), "htmlLink": created.get("htmlLink")}, indent=2))


# --------------------------------------------------------------------------
# Calendly (booking-link flow + polling for who's actually booked)
# --------------------------------------------------------------------------

class CalendlyNotConfigured(RuntimeError):
    """CALENDLY_API_TOKEN missing/broken. Same spirit as
    CalendarNotConfigured - never allowed to kill the whole run, just
    degrades (the poll step is skipped, a warning is printed) since this
    bot ran fine without Calendly before it was configured."""
    pass


def _calendly_headers():
    if not CALENDLY_API_TOKEN:
        raise CalendlyNotConfigured("CALENDLY_API_TOKEN is not set in .env")
    return {"Authorization": f"Bearer {CALENDLY_API_TOKEN}"}


def get_calendly_user_uri(state):
    """Calendly's API scopes /scheduled_events queries to a specific user
    (or organization) URI, not an email address - fetched once via
    /users/me and cached in state.json so a normal scan doesn't need this
    extra round trip every time."""
    cached = (state.get("calendly_user_uri") or "").strip()
    if cached:
        return cached
    import requests
    resp = requests.get(f"{CALENDLY_API_BASE}/users/me", headers=_calendly_headers(), timeout=15)
    resp.raise_for_status()
    data = resp.json()
    # Calendly wraps single-resource responses in a top-level "resource" key
    # - fall back to the bare dict too in case that ever changes.
    uri = ((data.get("resource") or {}).get("uri")) or data.get("uri")
    if not uri:
        raise CalendlyNotConfigured(f"unexpected /users/me response shape: {data!r}")
    state["calendly_user_uri"] = uri
    save_state(state)
    return uri


def poll_calendly_bookings(ws, header, rows, state):
    """Asks Calendly "who has an active booked event?" and marks any
    matching lead (by invitee email) status="booked" in Supabase. This is
    what replaces webhooks: the bot has no public URL for Calendly to call,
    so instead it asks on every scan (every ~20 min) - a booking is caught
    the first time a scan runs after it happens, not instantly, but that's
    fine for this use case.

    Returns how many leads were newly marked booked this run. Never raises
    - a Calendly hiccup (bad token, network blip, rate limit) just means
    this scan's poll is skipped and the next one tries again; it never
    takes down email sending/replying, which don't depend on this at all.
    """
    if not CALENDLY_API_TOKEN:
        return 0
    try:
        import requests
    except ImportError:
        print("  Calendly poll skipped: 'requests' isn't installed - run: pip install -r requirements.txt")
        return 0

    try:
        user_uri = get_calendly_user_uri(state)
    except Exception as e:
        print(f"  Calendly poll skipped: {e}")
        return 0

    lead_by_email = {
        _clean_email(r["email"]): r for r in rows if r.get("email")
    }
    if not lead_by_email:
        return 0

    # min_start_time filters by the MEETING's start time, not when it was
    # booked - Calendly's API has no "booked since X" filter, so instead we
    # just re-check a rolling window of upcoming/recent meeting times every
    # scan and skip anything already marked booked. Wide enough to catch a
    # meeting booked for "next month", small enough to stay a cheap query.
    lookback_days = int(os.getenv("CALENDLY_LOOKBACK_DAYS", "3"))
    lookahead_days = int(os.getenv("CALENDLY_LOOKAHEAD_DAYS", "90"))
    now = datetime.now(timezone.utc)
    min_start = (now - timedelta(days=lookback_days)).isoformat().replace("+00:00", "Z")
    max_start = (now + timedelta(days=lookahead_days)).isoformat().replace("+00:00", "Z")

    events = []
    page_token = None
    try:
        while True:
            params = {
                "user": user_uri,
                "status": "active",
                "min_start_time": min_start,
                "max_start_time": max_start,
                "count": 100,
            }
            if page_token:
                params["page_token"] = page_token
            resp = requests.get(f"{CALENDLY_API_BASE}/scheduled_events",
                                 headers=_calendly_headers(), params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            events.extend(data.get("collection") or [])
            page_token = ((data.get("pagination") or {}).get("next_page_token")) or None
            if not page_token:
                break
    except Exception as e:
        print(f"  Calendly poll failed fetching scheduled_events: {e}")
        return 0

    booked_count = 0
    for ev in events:
        ev_uri = ev.get("uri") or ""
        ev_uuid = ev_uri.rstrip("/").rsplit("/", 1)[-1] if ev_uri else ""
        if not ev_uuid:
            continue
        try:
            inv_resp = requests.get(
                f"{CALENDLY_API_BASE}/scheduled_events/{ev_uuid}/invitees",
                headers=_calendly_headers(), timeout=15,
            )
            inv_resp.raise_for_status()
            invitees = inv_resp.json().get("collection") or []
        except Exception as e:
            print(f"  Calendly poll: couldn't fetch invitees for event {ev_uuid}: {e}")
            continue

        for inv in invitees:
            if (inv.get("status") or "").strip().lower() == "canceled":
                continue
            inv_email = _clean_email(inv.get("email"))
            lead = lead_by_email.get(inv_email) if inv_email else None
            if not lead:
                continue
            if (lead.get("status") or "").strip().lower() == "booked":
                continue  # already recorded, nothing to do
            start_time = ev.get("start_time", "")
            update_lead(ws, header, lead["_row"], status="booked",
                        notes=f"booked via Calendly: {start_time}")
            log_activity("MEETING BOOKED (via Calendly)", lead.get("company"), lead["email"],
                         body=f"Calendly event at {start_time}\n{ev_uri}")
            print(f"  BOOKED (Calendly) {lead['email']} at {start_time}")
            booked_count += 1

    return booked_count


# --------------------------------------------------------------------------
# Learning loop: turn real outcomes (booked / declined-after-engaging) into
# permanent writing rules, automatically, every scan.
# --------------------------------------------------------------------------

_LOG_ENTRY_HEADER_RE = re.compile(
    r"^(?P<ts>.+?) — (?P<kind>.+?) — (?P<company>.*?) <(?P<email>.*?)>\s*$"
)


def _parse_activity_log():
    """Parses activity_log.md (written by log_activity(), append-only, so
    file order IS chronological order) into a list of
    {ts, kind, company, email, subject, body} dicts. Best-effort: a stray or
    hand-edited line just gets skipped rather than raising - this file is
    meant to be human-readable, not a strict machine format."""
    if not LOG_FILE.exists():
        return []
    text = LOG_FILE.read_text(encoding="utf-8")
    entries = []
    for chunk in text.split("\n## "):
        chunk = chunk.strip("\n")
        if not chunk:
            continue
        lines = chunk.splitlines()
        m = _LOG_ENTRY_HEADER_RE.match(lines[0])
        if not m:
            continue
        subject = ""
        body_lines = lines[1:]
        if body_lines and body_lines[0].startswith("**Subject:** "):
            subject = body_lines[0][len("**Subject:** "):].strip()
            body_lines = body_lines[1:]
        body = "\n".join(body_lines).strip()
        entries.append({
            "ts": m.group("ts"), "kind": m.group("kind"),
            "company": m.group("company"), "email": _clean_email(m.group("email")),
            "subject": subject, "body": body,
        })
    return entries


def _thread_text_for_email(email_addr, entries=None):
    """The full back-and-forth with one lead, formatted as plain text for a
    prompt - everything log_activity() ever recorded for that address, in
    order. Used to show the model what actually happened, not a summary of
    it."""
    if entries is None:
        entries = _parse_activity_log()
    target = _clean_email(email_addr)
    lines = []
    for e in entries:
        if e["email"] != target:
            continue
        lines.append(f"[{e['ts']}] {e['kind']}" + (f" — {e['subject']}" if e["subject"] else ""))
        if e["body"]:
            lines.append(e["body"])
        lines.append("")
    return "\n".join(lines).strip()


# --------------------------------------------------------------------------
# Daily recap: a plain-language summary of one day's activity (sent,
# replies, bookings, declines) with two headline percentages, meant for a
# PDF someone who never looks at the code or the dashboard can skim in 30
# seconds.
# --------------------------------------------------------------------------

# Every log_activity() "kind" string that means an email actually left the
# building. Drafts waiting for approval ("DRAFT (awaiting your approval)",
# "DRAFT referral (awaiting your approval)") are deliberately NOT in here -
# they haven't been sent yet, so they don't belong in a "sent today" count.
_RECAP_SENT_KINDS = {
    "SENT first email",
    "SENT (approved draft)",
    "SENT referral email",
    "SENT Calendly link",
    "SENT slot offer",
    "SENT alternatives (their proposed time was unavailable)",
    "SENT reply",
    "SENT reply (calendar unavailable - book manually)",
    "SENT closing reply (asked for a referral)",
    "SENT closing reply (not interested - marked dead)",
}
_RECAP_RECEIVED_KINDS = {"REPLY RECEIVED"}
_RECAP_BOOKED_KINDS = {
    "MEETING BOOKED",
    "MEETING BOOKED (via Calendly)",
    "MEETING BOOKED (lead's proposed time)",
}
# A lead saying "not interested" - NOT the same as "DEAD (liveness check
# failed)", which is the bot bouncing an invalid/dead address before ever
# reaching a human, not a real person declining.
_RECAP_DECLINED_KINDS = {"SENT closing reply (not interested - marked dead)"}


def build_daily_recap(date_str=None):
    """Summarizes one day's activity from activity_log.md.

    date_str: "YYYY-MM-DD", defaults to today. Matched as a plain string
    prefix against each entry's timestamp (log_activity() stamps entries
    with datetime.now().strftime("%Y-%m-%d %H:%M:%S") in local time, so this
    lines up exactly with "today" as the person running the bot sees it -
    no timezone math involved).

    Returns:
        {
          "date": "2026-09-24",
          "sent": [...], "received": [...], "booked": [...], "declined": [...],
          "contacted_today": 42,   # unique addresses with >=1 sent event today
          "closing_pct": 9.5,      # booked / contacted_today * 100 (0 if none)
          "decline_pct": 4.8,      # declined / contacted_today * 100 (0 if none)
        }

    Each list holds the matching entries from _parse_activity_log(), in
    chronological order. Note "SENT closing reply (not interested - marked
    dead)" entries show up in BOTH "sent" and "declined" - it genuinely is a
    sent email AND a decline, same as the bot's own status logic treats it.
    """
    date_str = date_str or datetime.now().strftime("%Y-%m-%d")
    entries = [e for e in _parse_activity_log() if e["ts"].startswith(date_str)]

    sent = [e for e in entries if e["kind"] in _RECAP_SENT_KINDS]
    received = [e for e in entries if e["kind"] in _RECAP_RECEIVED_KINDS]
    booked = [e for e in entries if e["kind"] in _RECAP_BOOKED_KINDS]
    declined = [e for e in entries if e["kind"] in _RECAP_DECLINED_KINDS]

    contacted_today = len({e["email"] for e in sent if e["email"]})
    closing_pct = (len(booked) / contacted_today * 100) if contacted_today else 0.0
    decline_pct = (len(declined) / contacted_today * 100) if contacted_today else 0.0

    return {
        "date": date_str,
        "sent": sent, "received": received, "booked": booked, "declined": declined,
        "contacted_today": contacted_today,
        "closing_pct": round(closing_pct, 1),
        "decline_pct": round(decline_pct, 1),
    }


def daily_activity_series(days=14):
    """Per-calendar-day counts of emails sent and meetings booked, straight
    from activity_log.md - real history for the dashboard's activity chart,
    not a fabricated trend. Returns a list of {date, sent, booked} dicts,
    oldest first, one entry per day for the last `days` days INCLUDING
    today, even for a day with zero activity (so the chart's x-axis has no
    gaps). Uses the same _RECAP_SENT_KINDS/_RECAP_BOOKED_KINDS classification
    build_daily_recap() uses, so this chart and the daily recap PDF always
    agree on what counts as "sent" or "booked"."""
    entries = _parse_activity_log()
    by_date = {}
    for e in entries:
        d = e["ts"][:10]
        bucket = by_date.setdefault(d, {"sent": 0, "booked": 0})
        if e["kind"] in _RECAP_SENT_KINDS:
            bucket["sent"] += 1
        if e["kind"] in _RECAP_BOOKED_KINDS:
            bucket["booked"] += 1

    today = datetime.now().date()
    out = []
    for i in range(days - 1, -1, -1):
        d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        b = by_date.get(d, {"sent": 0, "booked": 0})
        out.append({"date": d, "sent": b["sent"], "booked": b["booked"]})
    return out


def generate_daily_recap_pdf(date_str=None, out_path=None):
    """Renders build_daily_recap() as a one-glance PDF: headline numbers up
    top, then a table per category (sent / replies / booked / declined).
    Returns the Path it wrote to (default: daily_recap_<date>.pdf, right
    next to this script)."""
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.platypus import (
            HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
        )
    except ImportError:
        sys.exit(
            "The daily recap needs the 'reportlab' package - run:\n"
            "    pip install reportlab"
        )

    recap = build_daily_recap(date_str)
    out_path = Path(out_path) if out_path else BASE_DIR / f"daily_recap_{recap['date']}.pdf"

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "RecapTitle", parent=styles["Title"], fontSize=22, spaceAfter=2,
        textColor=colors.HexColor("#14161f"),
    )
    sub_style = ParagraphStyle(
        "RecapSub", parent=styles["Normal"], fontSize=11, spaceAfter=16,
        textColor=colors.HexColor("#6b6f7b"),
    )
    h2_style = ParagraphStyle(
        "RecapH2", parent=styles["Heading2"], fontSize=13, spaceBefore=16, spaceAfter=6,
        textColor=colors.HexColor("#14161f"),
    )
    body_style = ParagraphStyle("RecapBody", parent=styles["Normal"], fontSize=9.5, leading=13)
    head_style = ParagraphStyle("RecapHead", parent=styles["Normal"], fontSize=9.5, leading=13,
                                 textColor=colors.white)
    empty_style = ParagraphStyle(
        "RecapEmpty", parent=styles["Normal"], fontSize=9.5, leftIndent=2,
        textColor=colors.HexColor("#9a9ea8"),
    )
    metric_num_style = ParagraphStyle("RecapMetricNum", fontSize=17, leading=20, alignment=1,
                                       textColor=colors.HexColor("#14161f"))
    metric_label_style = ParagraphStyle("RecapMetricLabel", fontSize=7.5, leading=10, alignment=1,
                                         textColor=colors.HexColor("#6b6f7b"))

    doc = SimpleDocTemplate(
        str(out_path), pagesize=A4,
        topMargin=20 * mm, bottomMargin=18 * mm, leftMargin=18 * mm, rightMargin=18 * mm,
    )
    content_width = A4[0] - doc.leftMargin - doc.rightMargin
    story = []

    story.append(Paragraph("Daily Outreach Recap", title_style))
    story.append(Paragraph(recap["date"], sub_style))

    metrics = [
        ("Contacted today", str(recap["contacted_today"])),
        ("Sent", str(len(recap["sent"]))),
        ("Replies", str(len(recap["received"]))),
        ("Booked", str(len(recap["booked"]))),
        ("Declined", str(len(recap["declined"]))),
        ("Closing rate", f"{recap['closing_pct']}%"),
        ("Decline rate", f"{recap['decline_pct']}%"),
    ]
    metric_table = Table(
        [[Paragraph(f"<b>{v}</b>", metric_num_style) for _, v in metrics],
         [Paragraph(k, metric_label_style) for k, _ in metrics]],
        colWidths=[content_width / len(metrics)] * len(metrics),
    )
    metric_table.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.75, colors.HexColor("#e2e3e8")),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#eceef2")),
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f6f7f9")),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
    ]))
    story.append(metric_table)
    story.append(Spacer(1, 4 * mm))

    col_widths = [18 * mm, 42 * mm, 52 * mm, content_width - 112 * mm]

    def section(title, section_entries, color_hex):
        story.append(Paragraph(
            f'<font color="{color_hex}">●</font> {title} ({len(section_entries)})', h2_style
        ))
        story.append(HRFlowable(width="100%", thickness=0.6, color=colors.HexColor("#e2e3e8"),
                                 spaceAfter=6))
        if not section_entries:
            story.append(Paragraph("Nothing in this category today.", empty_style))
            story.append(Spacer(1, 2 * mm))
            return
        rows = [[
            Paragraph("Time", head_style), Paragraph("Company", head_style),
            Paragraph("Email", head_style), Paragraph("Details", head_style),
        ]]
        for e in section_entries:
            time_only = e["ts"].split(" ")[1] if " " in e["ts"] else e["ts"]
            detail = e["subject"] or e["body"] or e["kind"]
            if len(detail) > 220:
                detail = detail[:220].rsplit(" ", 1)[0] + "…"
            rows.append([
                Paragraph(time_only, body_style),
                Paragraph(e["company"] or "—", body_style),
                Paragraph(e["email"] or "—", body_style),
                Paragraph(detail.replace("\n", "<br/>"), body_style),
            ])
        t = Table(rows, colWidths=col_widths, repeatRows=1)
        t.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(color_hex)),
            ("LINEBELOW", (0, 1), (-1, -1), 0.4, colors.HexColor("#eceef2")),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ]))
        story.append(t)
        story.append(Spacer(1, 2 * mm))

    section("Sent", recap["sent"], "#5B8CFF")
    section("Replies received", recap["received"], "#22D3EE")
    section("Meetings booked", recap["booked"], "#34D399")
    section("Declined", recap["declined"], "#F87171")

    doc.build(story)
    return out_path


def cmd_daily_recap(args):
    out_path = generate_daily_recap_pdf(date_str=args.date, out_path=args.out)
    recap = build_daily_recap(args.date)
    print(
        f"Daily recap for {recap['date']}: {len(recap['sent'])} sent, "
        f"{len(recap['received'])} replies, {len(recap['booked'])} booked, "
        f"{len(recap['declined'])} declined - closing rate {recap['closing_pct']}%, "
        f"decline rate {recap['decline_pct']}%"
    )
    print(f"Saved to {out_path}")


def _load_lessons_text():
    """Raw contents of lessons.txt (both sections, whatever's in there) -
    empty string if the file doesn't exist yet (nothing learned so far)."""
    if not LESSONS_FILE.exists():
        return ""
    return LESSONS_FILE.read_text(encoding="utf-8").strip()


LESSON_SECTION_POSITIVE = "## Learned from what's worked (led to a booked meeting)"
LESSON_SECTION_NEGATIVE = "## Learned from what's gone wrong (declined after engaging)"


def _append_lesson(section_heading, rule_text, company, email_addr):
    """Appends one new rule under the right section of lessons.txt, creating
    the file/section the first time it's needed. Every line is dated and
    attributed to the lead it came from, so - just like icp.txt - you can
    open this file, see exactly why a rule is there, and delete/edit
    anything that turns out to be wrong or too narrow; nothing here is
    precious just because a model wrote it."""
    today = datetime.now().strftime("%Y-%m-%d")
    ref = f"{company}" if company else email_addr
    new_line = f"- ({today}, from {ref}) {rule_text}"

    if not LESSONS_FILE.exists():
        header = (
            "# Lessons learned from real outcomes — auto-maintained, edit freely\n\n"
            "This file is written to automatically by the learning loop in "
            "outreach_bot.py: every scan, it looks at leads that just booked a "
            "meeting or clearly declined after actually engaging, and asks "
            "Gemini to distill ONE specific, reusable rule from that real "
            "conversation. It's folded into every email's prompt alongside "
            "icp.txt from then on.\n\n"
            "Nothing here is sacred - if a rule looks wrong, too narrow, or "
            "just one weird lead rather than a real pattern, delete the line "
            "or edit it. This file will just keep growing new ones on its "
            "own either way.\n\n"
            f"{LESSON_SECTION_POSITIVE}\n\n{LESSON_SECTION_NEGATIVE}\n"
        )
        LESSONS_FILE.write_text(header, encoding="utf-8")

    text = LESSONS_FILE.read_text(encoding="utf-8")
    if section_heading not in text:
        # section header missing (e.g. hand-edited file) - add it at the end
        text = text.rstrip() + f"\n\n{section_heading}\n"
    lines = text.splitlines()
    insert_at = len(lines)
    for i, line in enumerate(lines):
        if line.strip() == section_heading:
            # insert right after this section's existing bullets, i.e.
            # before the next "## " heading (or end of file)
            insert_at = len(lines)
            for j in range(i + 1, len(lines)):
                if lines[j].startswith("## "):
                    insert_at = j
                    break
            break
    lines.insert(insert_at, new_line)
    # keep a blank line before the next "## " section heading, if any, so
    # sections don't visually run together as the file grows
    if insert_at + 1 < len(lines) and lines[insert_at + 1].startswith("## "):
        lines.insert(insert_at + 1, "")
    LESSONS_FILE.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def build_persona_text(icp_text):
    """The full persona block every composer prompt is built from: your
    hand-written icp.txt, plus whatever the learning loop has accumulated in
    lessons.txt so far. Centralized here so every call site (the "run" loop,
    test_reply_workflow.py, anything else) automatically picks up new
    lessons the moment they're written, without threading a second
    parameter through every compose_*() function."""
    lessons_text = _load_lessons_text()
    if not lessons_text:
        return icp_text
    return (
        f"{icp_text}\n\n"
        f"## Lessons learned from real outcomes (read carefully - these came "
        f"from actual leads' actual replies, not guesses - and take priority "
        f"over a generic instinct if they ever seem to conflict)\n"
        f"{lessons_text}\n"
    )


LEARNING_SYSTEM = """You study ONE finished real cold-outreach conversation \
(a salesperson's actual emails and a lead's actual replies) to extract AT \
MOST one crisp, reusable writing rule for future emails - the same terse, \
concrete, specific style already used for the existing rules below (a \
specific behavior to do or avoid, never vague advice like "be more \
engaging" or "build rapport").

Respond with ONLY a single JSON object (no markdown fences, no commentary):
  "has_lesson": true only if this specific conversation clearly points to a \
generalizable writing behavior - not just "this particular lead declined/ \
booked", but something about HOW the email was written or phrased that \
plausibly caused it. Many outcomes have nothing to do with writing quality \
(wrong timing, no budget, wrong department, they were never going to buy) - \
if you can't point to something concrete in the wording/approach itself, \
set this false and don't force a rule out of it.
  "rule": if has_lesson is true, ONE short rule (a single sentence or two, \
matching the bullet style of the existing rules), else null.

EXISTING RULES ALREADY IN FORCE (don't repeat these, and don't suggest \
anything that contradicts one without very strong evidence from THIS \
conversation specifically):
{existing_lessons}
"""


def distill_lesson(client, outcome_label, thread_text, existing_lessons):
    user_prompt = (
        f"OUTCOME: {outcome_label}\n\n"
        f"CONVERSATION THREAD (chronological, everything sent/received with this lead):\n"
        f"\"\"\"\n{thread_text}\n\"\"\"\n\n"
        f"Extract at most one new rule per the instructions."
    )
    system = LEARNING_SYSTEM.format(existing_lessons=existing_lessons or "(none yet)")
    return ask_gemini(client, system, user_prompt)


def run_learning_pass(client, rows, state):
    """Looks for leads that just reached a real outcome since the last scan
    and haven't been learned from yet, and distills at most
    LEARNING_BATCH_SIZE lessons per category (booked / declined) each run -
    capped so a big backlog doesn't burn a pile of API calls in one go; it
    just catches up gradually, one scan at a time. Returns how many new
    rules were actually added. Never raises - a Gemini hiccup here should
    never affect email sending, which doesn't depend on this at all."""
    if not LEARNING_ENABLED:
        return 0

    processed = set(state.setdefault("learning_processed", []))
    existing_lessons = _load_lessons_text()
    entries = _parse_activity_log()

    booked = [
        r for r in rows
        if (r.get("status") or "").strip().lower() == "booked"
        and _clean_email(r.get("email")) not in processed
    ][:LEARNING_BATCH_SIZE]
    declined = [
        r for r in rows
        if (r.get("status") or "").strip().lower() == "dead"
        and (r.get("notes") or "").strip().lower().startswith("marked not interested")
        and _clean_email(r.get("email")) not in processed
    ][:LEARNING_BATCH_SIZE]

    added = 0
    for lead, outcome_label, section in (
        [(r, "BOOKED - this lead scheduled a meeting", LESSON_SECTION_POSITIVE) for r in booked]
        + [(r, "DECLINED - this lead clearly said no after actually engaging in conversation",
            LESSON_SECTION_NEGATIVE) for r in declined]
    ):
        email_addr = _clean_email(lead.get("email"))
        thread_text = _thread_text_for_email(email_addr, entries)
        if not thread_text:
            processed.add(email_addr)  # nothing to learn from, don't keep retrying
            continue
        try:
            result = distill_lesson(client, outcome_label, thread_text, existing_lessons)
        except Exception as e:
            print(f"  Learning pass: couldn't distill a lesson for {email_addr}: {e}")
            continue  # don't mark processed - worth retrying next scan
        processed.add(email_addr)
        if result.get("has_lesson") and (result.get("rule") or "").strip():
            rule = result["rule"].strip()
            _append_lesson(section, rule, lead.get("company"), email_addr)
            existing_lessons = _load_lessons_text()  # refresh so the next lead in this batch sees it too
            kind = "LEARNED (from a booked meeting)" if section == LESSON_SECTION_POSITIVE \
                else "LEARNED (from a decline)"
            log_activity(kind, lead.get("company"), email_addr, body=rule)
            print(f"  LEARNED (from {email_addr}, {outcome_label.split(' - ')[0].lower()}): {rule}")
            added += 1

    state["learning_processed"] = sorted(processed)
    return added


# --------------------------------------------------------------------------
# run: the actual loop — pull, draft with the LLM, decide on a meeting,
# send. This is the one command you put in Task Scheduler/cron.
#
# get_gemini_client()/ask_gemini() are the ONLY two functions in this file
# that know which LLM provider is actually being used - every composer
# (compose_first_email, compose_reply, etc.) just calls them by these names
# regardless of provider, so LLM_PROVIDER in .env is the only thing that
# needs to change to switch providers. Kept the "gemini" names for that
# reason even though they now also cover Groq.
# --------------------------------------------------------------------------

def get_gemini_client():
    if LLM_PROVIDER == "claude":
        if not ANTHROPIC_API_KEY:
            sys.exit(
                "ANTHROPIC_API_KEY is not set in .env (LLM_PROVIDER=claude). "
                "Get one at console.anthropic.com/settings/keys, add billing "
                "there, and add the key to .env."
            )
        return {"provider": "claude"}
    if LLM_PROVIDER == "groq":
        if not GROQ_API_KEY:
            sys.exit(
                "GROQ_API_KEY is not set in .env (LLM_PROVIDER=groq). Get "
                "one free, no credit card, at console.groq.com/keys, and "
                "add it to .env."
            )
        return {"provider": "groq"}
    if LLM_PROVIDER != "gemini":
        sys.exit(f"Unknown LLM_PROVIDER={LLM_PROVIDER!r} in .env - use 'claude', 'gemini', or 'groq'.")
    if not GEMINI_API_KEY:
        sys.exit(
            "GEMINI_API_KEY is not set in .env. Get one free at "
            "aistudio.google.com/apikey, and add it to .env - or switch "
            "providers with LLM_PROVIDER in .env (claude/groq)."
        )
    from google import genai
    return {"provider": "gemini", "client": genai.Client(api_key=GEMINI_API_KEY)}


def _repair_unescaped_quotes(raw):
    """Best-effort fix for the single most common way the model's JSON
    breaks: a literal, unescaped '"' INSIDE a string value instead of a
    real string terminator - almost always a Hebrew abbreviation mark like
    בע"מ (Ltd.) or מנכ"ל (CEO)/סמנכ"ל (VP), which are everywhere in company
    names and job titles this bot handles. json.loads then fails with
    something like "Expecting ',' delimiter" right after that character.

    Walks the raw text tracking whether we're inside a string, and treats a
    '"' as a REAL terminator only if - skipping any whitespace - the next
    character is one a JSON string can actually be followed by (, } ] :
    or end of input); otherwise it's escaped instead, on the assumption
    it's content like בע"מ, not the end of the value. Not a full JSON
    parser, just enough of one to fix this one specific, very common
    shape of breakage - never raises, worst case the output is unchanged
    and json.loads fails again the same way it would have anyway."""
    out = []
    in_string = False
    i, n = 0, len(raw)
    while i < n:
        ch = raw[i]
        if in_string and ch == "\\" and i + 1 < n:
            out.append(ch)
            out.append(raw[i + 1])
            i += 2
            continue
        if ch == '"':
            if not in_string:
                in_string = True
                out.append(ch)
            else:
                j = i + 1
                while j < n and raw[j] in " \t\r\n":
                    j += 1
                if j >= n or raw[j] in ",}]:":
                    in_string = False
                    out.append(ch)
                else:
                    out.append('\\"')
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def ask_gemini(client, system_prompt, user_prompt, _retries=6):
    provider = client.get("provider", "gemini") if isinstance(client, dict) else "gemini"
    if provider == "claude":
        text = _ask_claude(system_prompt, user_prompt, _retries=_retries)
    elif provider == "groq":
        text = _ask_groq(system_prompt, user_prompt, _retries=_retries)
    else:
        text = _ask_gemini_raw(client["client"], system_prompt, user_prompt, _retries=_retries)

    # Asked for JSON directly (response_mime_type for Gemini, response_format
    # for Groq), but be lenient about any stray text/markdown fences anyway -
    # Claude just gets told in the prompt to respond with only JSON, same as
    # the other two, since every composer's system prompt already says so.
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"No JSON object in model output: {text[:300]}")
    raw = text[start:end + 1]
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        # Very likely a stray literal quote inside a string value (בע"מ,
        # מנכ"ל, and similar Hebrew abbreviations are common in this bot's
        # own data) - try the targeted repair above before giving up.
        try:
            return json.loads(_repair_unescaped_quotes(raw))
        except json.JSONDecodeError:
            raise ValueError(f"Model returned invalid JSON ({e}): {raw[:500]}")


def _ask_claude(system_prompt, user_prompt, _retries=6):
    """Same job as _ask_gemini_raw/_ask_groq but against Anthropic's Messages
    API - plain requests, no extra SDK dependency. system is a top-level
    field (not a role:"system" message) per Anthropic's API shape."""
    import requests

    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    payload = {
        "model": CLAUDE_MODEL,
        # See GEMINI_MAX_OUTPUT_TOKENS - same reasoning, kept in sync so
        # switching LLM_PROVIDER doesn't reintroduce the truncated-JSON
        # failure mode on a big batch call.
        "max_tokens": GEMINI_MAX_OUTPUT_TOKENS,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}],
    }
    for attempt in range(_retries):
        resp = requests.post(url, headers=headers, json=payload, timeout=60)
        # 429 = rate limit, 5xx/overloaded = transient - worth backing off
        # and retrying. Anything else (400/401/403/404 - bad request, bad
        # key, or CLAUDE_MODEL naming a model that doesn't exist) is a
        # CONFIG problem, not a transient one, so raise immediately with the
        # response body instead of wasting ~2 minutes retrying the same
        # failure.
        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt == _retries - 1:
                if resp.status_code == 429:
                    raise RuntimeError(
                        f"Anthropic rate limit still hit after {_retries} "
                        f"retries: {resp.text[:500]} - check your usage/limits "
                        f"at console.anthropic.com, or lower BATCH_SIZE in .env."
                    )
                resp.raise_for_status()
            retry_after = resp.headers.get("retry-after")
            try:
                wait = float(retry_after) if retry_after is not None else None
            except ValueError:
                wait = None
            if wait is None:
                wait = min(5 * (2 ** attempt), 60)
            print(f"  Claude {resp.status_code} - retrying in {wait:.0f}s "
                  f"(attempt {attempt + 1}/{_retries})...")
            time.sleep(wait)
            continue
        if not resp.ok:
            raise RuntimeError(
                f"Anthropic API error {resp.status_code} for model {CLAUDE_MODEL!r}: "
                f"{resp.text[:500]} - check CLAUDE_MODEL and ANTHROPIC_API_KEY in "
                f".env (current models: platform.claude.com/docs/en/models/overview)."
            )
        data = resp.json()
        parts = data.get("content") or []
        return "".join(p.get("text", "") for p in parts if p.get("type") == "text")


def _ask_gemini_raw(genai_client, system_prompt, user_prompt, _retries=6):
    from google.genai import errors as genai_errors

    for attempt in range(_retries):
        try:
            resp = genai_client.models.generate_content(
                model=GEMINI_MODEL,
                config={
                    "system_instruction": system_prompt,
                    "response_mime_type": "application/json",
                    # Left unset, some Gemini SDK/model combos cap generation
                    # well short of what a big batch call (classify/compose
                    # several leads in one JSON response) actually needs - a
                    # response cut off mid-generation comes back as
                    # truncated, invalid JSON, which reads exactly like a
                    # "nothing matched" answer instead of the token-limit
                    # problem it actually is. Explicit and generous instead.
                    "max_output_tokens": GEMINI_MAX_OUTPUT_TOKENS,
                },
                contents=user_prompt,
            )
            return resp.text or ""
        except genai_errors.ServerError as e:
            # Transient overload (503 "high demand") or similar 5xx - worth
            # retrying with backoff rather than losing this whole reply (and,
            # since the IMAP bookmark has already moved past it by the time
            # we get here, forcing a manual rewind_bookmark.py to see it
            # again). Google's "high demand" spikes can outlast a couple of
            # quick retries, so this backs off further each time: 5, 10, 20,
            # 40, 60s (~2.25 minutes total) before finally giving up.
            if attempt == _retries - 1:
                raise
            wait = min(5 * (2 ** attempt), 60)
            print(f"  Gemini server error ({e}) - retrying in {wait}s "
                  f"(attempt {attempt + 1}/{_retries})...")
            time.sleep(wait)


def _ask_groq(system_prompt, user_prompt, _retries=6):
    """Same job as _ask_gemini_raw but against Groq's OpenAI-compatible
    chat-completions endpoint - plain requests, no extra SDK dependency."""
    import requests

    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "response_format": {"type": "json_object"},
    }
    for attempt in range(_retries):
        resp = requests.post(url, headers=headers, json=payload, timeout=60)
        # 429 = rate limit (free-tier RPD/RPM cap), 5xx = transient
        # server-side issue - both worth backing off and retrying rather
        # than losing this reply outright. Anything else (400/401/403/404 -
        # bad request, bad key, or GROQ_MODEL naming a model that doesn't
        # exist/was retired) is a CONFIG problem, not a transient one -
        # retrying it 6 times with backoff just wastes ~2 minutes before
        # failing the same way, so raise immediately with the response body
        # (Groq's error messages are specific, e.g. "the model ... does not
        # exist") so it's obvious what to fix in .env.
        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt == _retries - 1:
                if resp.status_code == 429:
                    raise RuntimeError(
                        f"Groq rate limit still hit after {_retries} retries: "
                        f"{resp.text[:500]} - the free tier caps every model "
                        f"at 8,000 tokens/minute (shared across RPM/TPM/TPD "
                        f"limits together), so a couple of back-to-back calls "
                        f"with icp.txt+lessons.txt in the system prompt can "
                        f"burn through it fast. This isn't a bug to fix in "
                        f"code - either wait a bit and re-run, trim icp.txt/"
                        f"lessons.txt, lower BATCH_SIZE, or switch back to "
                        f"LLM_PROVIDER=gemini for this run."
                    )
                resp.raise_for_status()
            # Groq's 429s include a "retry-after" header with the ACTUAL
            # seconds until that specific limit resets - trust it over a
            # guessed backoff when it's there, since the free tier's tight
            # per-minute token cap (8,000 TPM, shared across every model)
            # can take longer to clear than a fixed 5/10/20/40/60s schedule
            # would wait, especially with icp.txt+lessons.txt in every
            # system prompt.
            retry_after = resp.headers.get("retry-after")
            try:
                wait = float(retry_after) if retry_after is not None else None
            except ValueError:
                wait = None
            if wait is None:
                wait = min(5 * (2 ** attempt), 60)
            print(f"  Groq {resp.status_code} - retrying in {wait:.0f}s "
                  f"(attempt {attempt + 1}/{_retries})...")
            time.sleep(wait)
            continue
        if not resp.ok:
            raise RuntimeError(
                f"Groq API error {resp.status_code} for model {GROQ_MODEL!r}: "
                f"{resp.text[:500]} - check GROQ_MODEL and GROQ_API_KEY in .env "
                f"(current models: console.groq.com/docs/models)."
            )
        data = resp.json()
        return data["choices"][0]["message"]["content"] or ""


# --------------------------------------------------------------------------
# company research: scrape the lead's own website so the composer has
# something real and specific to work with, instead of writing to a
# company name and nothing else.
# --------------------------------------------------------------------------

def _normalize_website(raw):
    """Turns whatever's in the sheet's website cell (or a bare domain
    pulled from an email address) into a fetchable https:// URL. Returns
    None for blank/junk input rather than raising - a lead with nothing
    usable here just skips research, it doesn't stop the run."""
    if not raw:
        return None
    raw = str(raw).strip()
    if not raw or "." not in raw:
        return None
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    return raw


def _html_to_text(html):
    """Strips a raw HTML string down to plain visible text. Shared by both
    the crawl4ai HTML fallback and the plain-requests scraper."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        print("Skipping company research: 'beautifulsoup4' isn't installed - "
              "run: pip install -r requirements.txt")
        return None
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "img"]):
        tag.decompose()
    text = " ".join(soup.get_text(separator=" ").split())
    return text or None


def _fetch_via_crawl4ai(url, timeout, max_chars):
    """Uses a self-hosted Crawl4AI Docker server (CRAWL4AI_URL) instead of a
    plain HTTP fetch - handles JS-rendered sites and gives back markdown
    that's already fairly clean. Returns None on any failure so the caller
    can fall back to the plain scraper rather than losing the lead."""
    try:
        import requests
    except ImportError:
        print("Skipping company research: 'requests' isn't installed - "
              "run: pip install -r requirements.txt")
        return None

    headers = {}
    if CRAWL4AI_API_TOKEN:
        headers["Authorization"] = f"Bearer {CRAWL4AI_API_TOKEN}"

    try:
        resp = requests.post(
            f"{CRAWL4AI_URL}/crawl",
            json={
                "urls": [url],
                "crawler_config": {
                    "type": "CrawlerRunConfig",
                    "params": {"cache_mode": "bypass", "stream": False},
                },
            },
            headers=headers,
            timeout=timeout,
        )
        if resp.status_code == 401:
            print(f"  crawl4ai ({CRAWL4AI_URL}) returned 401 Unauthorized for {url} - "
                  f"your Crawl4AI server has API-key auth on. Set CRAWL4AI_API_TOKEN "
                  f"in .env to the token it expects, or disable auth on the server.")
            return None
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  crawl4ai ({CRAWL4AI_URL}) failed for {url}: {e}")
        return None

    results = data.get("results") or []
    if not results:
        print(f"  crawl4ai returned no results for {url}")
        return None
    result = results[0]
    if not result.get("success", True):
        print(f"  crawl4ai couldn't crawl {url}: {result.get('error_message', 'unknown error')}")
        return None

    # Different crawl4ai versions shape "markdown" differently (a plain
    # string vs. an object with raw_markdown/fit_markdown) - handle both,
    # then fall back to extracted_content, then raw/cleaned HTML.
    md = result.get("markdown")
    text = None
    if isinstance(md, str):
        text = md
    elif isinstance(md, dict):
        text = md.get("fit_markdown") or md.get("raw_markdown") or md.get("markdown_with_citations")
    if not text:
        text = result.get("extracted_content")
    if not text:
        html = result.get("cleaned_html") or result.get("html")
        if html:
            text = _html_to_text(html)

    if not text:
        return None
    text = " ".join(str(text).split())
    return text[:max_chars] if text else None


def fetch_website_text(url, timeout=10, max_chars=6000):
    """Best-effort scrape of a company's homepage, stripped down to plain
    text. Uses your Crawl4AI server (CRAWL4AI_URL in .env) if configured,
    otherwise a plain requests+BeautifulSoup fetch. Returns None (never
    raises) on anything that goes wrong - a dead, blocked, or slow site
    just means we skip research for that lead this time, not that the
    whole run should fail."""
    if CRAWL4AI_URL:
        text = _fetch_via_crawl4ai(url, timeout=max(timeout, 30), max_chars=max_chars)
        if text:
            return text
        print(f"  falling back to a plain HTTP fetch for {url}")

    try:
        import requests
    except ImportError:
        print("Skipping company research: 'requests' isn't installed - "
              "run: pip install -r requirements.txt")
        return None

    try:
        resp = requests.get(
            url, timeout=timeout, allow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; outreach-research/1.0)"},
        )
        resp.raise_for_status()
    except Exception as e:
        print(f"  couldn't fetch {url}: {e}")
        return None

    text = _html_to_text(resp.text)
    return text[:max_chars] if text else None


RESEARCH_SYSTEM = """You read a company's own website text and summarize it \
for a salesperson about to write them a cold email. Respond with ONLY a \
single JSON object (no markdown fences, no commentary):
  "summary": 2-4 plain sentences on what this company actually does, its \
scale/industry, and anything concrete on the page worth referencing by name \
(a facility type, a service line, a product line, a location, an event, a \
certification) so a first email can reference something real instead of \
reading like it was sent to anyone. A concrete product or service the \
company sells/does (e.g. "they rent shipping containers and sell medical \
refrigeration equipment") already counts as something worth referencing - \
state it plainly and stop there. \
CRITICAL: never undercut facts you just stated. If you wrote down ANY \
concrete fact about what the company does, do not also add a disclaimer \
saying there's nothing specific/concrete to reference - that directly \
contradicts what you just wrote and will cause the email this feeds into \
to wrongly skip mentioning it. Only say the page was too thin to say \
anything specific in the narrow case where you genuinely found NOTHING \
concrete at all (e.g. the page really is just a nav menu / logo / contact \
form with no description of what the company does) - and even then, keep \
that one short sentence and don't list out everything that was missing.
"""


def research_company(client, company, website_text):
    user_prompt = (
        f"Company: {company}\n\nScraped homepage text:\n\"\"\"\n{website_text}\n\"\"\"\n\n"
        f"Summarize per the instructions."
    )
    result = ask_gemini(client, RESEARCH_SYSTEM, user_prompt)
    return (result.get("summary") or "").strip()


# --------------------------------------------------------------------------
# Website prefetching - fills personal_emails.raw_scrape AHEAD OF TIME, so a
# later `run` never has to wait on (or pay for) a live website fetch during
# the actual outreach scan. Deliberately NO Gemini call in here at all -
# just fetch_website_text() (network I/O only) - summarizing/using the raw
# text happens later, inside compose_person_emails_batch(), as part of the
# same call that writes the email. Meant to run on its own schedule (its
# own cron entry, e.g. overnight or every few hours), separately from
# `run`, so a slow or unreachable site never eats into the outreach run's
# own time budget.
# --------------------------------------------------------------------------

def prefetch_raw_scrapes(count):
    """Fetches and caches raw website text for up to `count` personal_emails
    rows that don't have one yet (oldest "new" rows first, skipping
    dead/rejected ones - same eligibility idea as the rest of this file's
    backfill passes). Returns how many rows were newly filled in. Never
    raises - one lead's fetch failing (dead site, timeout, no website on
    file at all) just means it's skipped and retried on a future prefetch
    run, same as every other network-dependent step in this file.

    Locking: only the initial read and each row's write are done under
    LEADS_LOCK - the slow part (fetch_website_text, one HTTP/crawl4ai
    fetch per candidate, which is where nearly all of the wall-clock time
    here goes) deliberately runs with the lock released. This function
    used to be called with the whole thing wrapped in one `with
    LEADS_LOCK:` block by its caller, which meant the dashboard's
    /api/status endpoint (get_pipeline(), which only ever tries a
    non-blocking lock acquire) kept losing the race and serving a stale
    cached snapshot for the entire prefetch - looking to the user like the
    dashboard "hangs" for 20-30 seconds right after a run finishes, even
    though the run itself had already completed. Taking the lock only
    around the actual Supabase read/write means /api/status can get a
    fresh snapshot between any two candidates instead of only after all of
    them are done."""
    with LEADS_LOCK:
        _, ws, header, rows = load_leads()
    candidates = [
        r for r in rows
        if r.get("_table") == SUPABASE_PERSON_TABLE
        and (r.get("status") or "").strip().lower() not in ("dead", "rejected")
        and not (r.get("raw_scrape") or "").strip()
    ][:count]

    filled = 0
    for r in candidates:
        label = r.get("contact_name") or r["email"]
        website = _normalize_website(r.get("company_website"))
        if not website and "@" in (r.get("email") or ""):
            website = _normalize_website(r["email"].split("@", 1)[1])
        if not website:
            print(f"  SKIP {label}: no company_website on file and no usable "
                  f"email domain to derive one from.")
            continue
        # Deliberately NOT under LEADS_LOCK - this is the slow network call
        # (page fetch, crawl4ai retries/anti-bot fallbacks) and holding the
        # lock across it is what used to freeze the dashboard's data.
        website_text = fetch_website_text(website)
        if not website_text:
            print(f"  {label} ({website}): fetch came back empty - will retry "
                  f"on a future prefetch run.")
            continue
        try:
            with LEADS_LOCK:
                update_lead(ws, header, r["_row"], company_website=website, raw_scrape=website_text)
        except Exception as e:
            print(f"  couldn't persist raw_scrape for {r['email']} ({e}) - have you "
                  f"run the raw_scrape ALTER TABLE (see add_raw_scrape_column.sql)?")
            continue
        filled += 1
        print(f"  prefetched {website} for {label} ({len(website_text)} chars)")
    return filled


COMPOSER_SYSTEM = """You are writing cold outreach emails as a real salesperson \
at the company described below. Stay strictly in character and never mention \
that you are an AI. Follow the persona, offer, and rules exactly.

LANGUAGE: write the "subject" and "body" in {language} - natural, fluent, \
native-sounding {language}, never a stiff word-for-word translation. \
Exception: if the lead's own message you're replying to (given below) is \
written in a clearly different language than {language}, mirror THEIR \
language instead for this reply, so you never answer someone in a language \
they didn't use with you.

GRAMMAR CHECK (when writing Hebrew): before answering, re-read the "subject" \
and "body" you wrote and fix any grammar mistake, especially these common \
ones: (1) infinitive verbs always keep their "ל" prefix - never write a \
bare verb like "קחת"/"שמוע"/"ראות" where the correct form is \
"לקחת"/"לשמוע"/"לראות"; (2) when one word governs a list of several items \
with a prefix like "ב-"/"ל-"/"מ-", that SAME prefix must repeat on every \
item in the list - never drop it or switch to "ה-"/"ו-" partway through; \
(3) subject-verb number agreement - a plural subject ("הם"/"הן"/"אתם"/a \
plural company name) always takes a plural verb, e.g. "הם לא מעבירים" \
never "הם לא מעביר"; (4) grammatical number agreement between a numeral \
and its noun - "מיליוני קילומטרים"/"אלפי לקוחות" (plural noun), never \
"מיליוני קילומטר"/"אלפי לקוח" (singular noun after a plural-implying \
numeral); (5) consistent formality/person throughout the whole email - if \
you're addressing the recipient as "אתם"/"לכם" (plural/formal, matching a \
company), never switch mid-email to "אתה"/"לך" (singular), and vice \
versa - pick one and keep it everywhere, including in blanks other than \
this one. Never send a sentence with a spelling or grammar error - if \
you're not sure a sentence is fully correct, simplify it rather than risk \
an error.

--- PERSONA / ICP ---
{icp}
--- END PERSONA ---

For every reply you give, respond with ONLY a single JSON object (no markdown \
fences, no commentary) with these fields:
  "subject": string
  "body": string (plain text email, no signature block needed beyond a first name)
  "wants_meeting": true if the lead is showing real interest in talking further, \
else false
  "picked_slot_index": integer index (0-based) into the OFFERED SLOTS list if the \
lead's reply clearly confirms one of them, else null
  "proposed_datetime": if the lead's message itself proposes a specific new \
day/time for a call - e.g. "does tomorrow at 4pm work?" or "Thursday morning" \
(pick a reasonable specific time like 10:00 for a vague part-of-day) - that is \
NOT one of the OFFERED SLOTS already listed, a string "YYYY-MM-DD HH:MM" in \
24-hour LOCAL time, resolved against the "right now" timestamp given in the \
prompt, else null. Only set this from a time THEY suggested, never one you \
invent, and never set it at the same time as picked_slot_index.
  "not_interested": true if their reply is a clear "no" - they're not a fit \
(wrong kind of business, no need for what we sell, explicitly say not \
relevant/interested), a firm decline, or an explicit unsubscribe/stop-emailing \
request, else false. This ends the conversation on our side - be sure before \
setting it true: a real objection, a question, "not right now but check back \
later", or any sign they might still be open to it later is NOT \
not_interested (keep the door open, set it false). But a flat "we don't have \
X" / "not relevant to us" / "no thanks" deserves a short, gracious one-line \
close (not a generic "let me know if that changes" - already said once, \
don't repeat that promise on every reply), and this field true. \
CRITICAL for this close specifically: it must still sound like YOU (the \
persona above) personally replying to ONE person, not an automated \
unsubscribe/opt-out confirmation. Never say or imply "removed you from the \
[mailing/distribution] list," "רשימת התפוצה," "unsubscribed," or anything \
that admits this was a mass/bulk send - that's the single most robotic, \
system-generated-sounding thing you can write, and the opposite of the \
"sound like a real person" rules above, which apply to this reply just as \
much as a first email. Address them as an individual ("you", second person \
singular - in Hebrew that's the plain "אתה/את", never the plural "אתם"/ \
"אתכם" unless they explicitly wrote as a company/group themselves), and \
keep it to something a person would actually type back - "no worries, \
thanks for letting me know" / "all good, appreciate you saying so" (or the \
Hebrew equivalent in the same register) - not a formal confirmation of any \
kind.
  "redirect_email": if their reply hands you off to someone else to contact \
instead - a colleague, the actual decision-maker, "not me, try X" - and gives \
a specific email address for that person, the exact email address string \
(copied exactly as written, lowercase is fine), else null. Only set this from \
an address THEY explicitly gave in this message, never one you infer or recall \
from elsewhere. A vague "talk to our IT manager" with no address given is NOT \
enough - leave it null until an actual address is provided. This can be set \
alongside any of the other fields above (e.g. they decline for themselves but \
redirect you, or they confirm a meeting for themselves AND redirect you to loop \
someone else in) - it's independent of the rest.
  "ask_for_referral": true only if not_interested is ALSO true, redirect_email \
is null (they didn't already hand you an address), and something about how \
they declined suggests they personally aren't the right contact but someone \
else there might be relevant (e.g. they said it's not their department/ \
decision, not "we as a company have no use for this at all"). When true, the \
closing "body" you write should - in the SAME natural message, not a bolted-on \
extra line - also lightly ask if there's someone else there it'd make sense to \
loop in, and to feel free to send their email if so. Leave this false when the \
decline is a flat "we don't need this kind of thing" - asking for a referral to \
someone else at a company that flatly doesn't need the product/service makes no \
sense and reads as not listening.
  "alert_needed": true if this reply needs YOUR judgment before anything more \
is sent - don't try to handle it yourself. Set this true broadly, whenever \
you're genuinely unsure how to classify or respond appropriately, not just for \
the most obvious cases. Clear examples: a hostile or angry tone; anything that \
reads as a complaint about how they were contacted; a legal, compliance, GDPR/ \
privacy, "cease and desist," or "I'll report this" mention; a request clearly \
outside a normal sales conversation (e.g. a job application, a security/vendor- \
risk questionnaire, press/media asking questions, someone claiming to be a \
regulator); anything that seems like it could be a phishing/social-engineering \
attempt directed at YOU; or - just as importantly - any reply where you're not \
confident the normal fields above (wants_meeting/not_interested/redirect_email/ \
etc.) correctly capture what's actually going on. When true: still fill in the \
other fields with your best guess (they won't be acted on, but give your best \
read), and leave "subject"/"body" as a best-effort draft too - NOTHING gets \
sent automatically when alert_needed is true, so there's no harm in drafting \
something, but don't spend extra effort perfecting it.
  "alert_reason": if alert_needed is true, ONE short plain sentence explaining \
specifically why - this goes straight into an email to the person running this \
bot, so make it something they can act on immediately (e.g. "angry tone, \
accuses us of spam and threatens to report it" or "asks a detailed compliance/ \
security questionnaire the bot shouldn't answer on its own"), else null.
"""


# --------------------------------------------------------------------------
# Slotted composer for first-touch emails (first cold email + referral
# handoff email). Instead of asking the model for one free-text "body"
# blob, it's asked for named parts (opening / pitch / sign-off) that CODE
# then joins in a fixed order - the shape is locked and auditable (each
# part can be checked/logged on its own, and the banned-phrase backstop
# runs over the assembled result before anything is sent), while the actual
# wording of each part is still written fresh per lead, not copy-pasted
# boilerplate - so it stays natural rather than becoming a mad-lib.
# --------------------------------------------------------------------------

FIRST_EMAIL_SLOTTED_SYSTEM = """You are writing a FIRST-touch cold outreach \
email as a real salesperson at the company described below. Stay strictly \
in character and never mention that you are an AI. Follow the persona, \
offer, and rules exactly.

LANGUAGE: write every field (subject, opening, pitch) in {language} - \
natural, fluent, native-sounding {language}, never a stiff word-for-word \
translation. signoff_name stays just a first name either way, in whichever \
script that name is normally written in.

--- PERSONA / ICP ---
{icp}
--- END PERSONA ---

Respond with ONLY a single JSON object (no markdown fences, no commentary). \
The email is broken into separate parts rather than one blob of text - each \
part is still real, natural prose written fresh for this lead (never \
copy-paste boilerplate, never reuse the same opening across different \
leads), just kept in separate fields so the parts that matter can be \
checked individually before this gets sent:
  "subject": string
  "opening": string - the first 1-2 sentences. The specific hook or \
observation that makes this obviously written for THIS lead, not a stock \
opener ("I hope this email finds you well" and equivalents are always \
wrong here). If RECIPIENT TYPE (given in the prompt) is "generic_inbox", \
this can just be a brief one-line "hey" style opener instead - don't force \
a personalized hook onto a request to be pointed to the right person.
  "pitch": string - 1-3 sentences. If RECIPIENT TYPE is "specific_person": \
the actual short pitch, per the persona/offer above, optionally weaving in \
ONE specific researched detail if one was given (never invented). If \
RECIPIENT TYPE is "generic_inbox": this is NOT a pitch - it's a short, \
direct ask to be pointed to whoever handles this at their organization, \
per the persona's rules for a generic inbox. Never pitch product/service \
details to a generic inbox.
  "signoff_name": string - just a first name (e.g. "Itay" or "איתי"), \
nothing else - no title, no company name, no "Best,"/"Regards,"/equivalent \
word before it.
"""


def _assemble_slotted_email(slots, part_keys, fallback_subject, fallback_body):
    """Joins named prose parts (in order) into one {"subject", "body"}
    dict, double-newline separated - and runs the banned-phrase backstop
    over the result. If a banned phrase shows up anyway (the model ignored
    the instruction), this does NOT send the offending text - it swaps in
    the safe fallback instead and prints a loud warning, so a prompt-level
    miss can never silently reach a lead's inbox."""
    subject = (slots.get("subject") or "").strip() or fallback_subject
    parts = [(slots.get(k) or "").strip() for k in part_keys]
    body = "\n\n".join(p for p in parts if p) or fallback_body
    hit = find_banned_phrase(subject, body)
    if hit:
        print(f"  WARNING: a composed email contained a banned phrase ({hit!r}) "
              f"despite the prompt instruction - using a safe fallback instead "
              f"of sending that text. Worth checking if this keeps happening.")
        subject, body = fallback_subject, fallback_body
    return {"subject": subject, "body": body}


def compose_first_email(client, icp_text, company, email_addr, research="",
                         recipient_kind="specific_person"):
    research_block = research.strip() if research and research.strip() else (
        "(no research available for this one - write a good first email using "
        "just the ICP rules, don't invent company specifics)"
    )
    kind_note = (
        "generic_inbox - this looks like a shared/role inbox (info@, support@, "
        "office@, etc.), not a specific person. Do NOT pitch - just ask briefly "
        "to be pointed to the right person, per the persona's rules for this case."
        if recipient_kind == "generic_inbox" else
        "specific_person - this looks like it belongs to a named individual. "
        "The normal pitch applies."
    )
    user_prompt = (
        f"RECIPIENT TYPE: {kind_note}\n\n"
        f"Company: {company}\nEmail address it's going to: {email_addr}\n\n"
        f"What we found about this company from their own website:\n"
        f"\"\"\"\n{research_block}\n\"\"\"\n\n"
        f"This is a first touch - keep it short, per the rules. If the research "
        f"above gives you something concrete and the recipient is a specific "
        f"person, weave in ONE specific detail naturally (not a list of facts) "
        f"so this clearly wasn't sent to a hundred companies unchanged - that's "
        f"what actually gets a reply. Never invent a detail that isn't in the "
        f"research."
    )
    slots = ask_gemini(client, FIRST_EMAIL_SLOTTED_SYSTEM.format(icp=icp_text, language=EMAIL_LANGUAGE), user_prompt)
    signoff = (slots.get("signoff_name") or "").strip() or "—"
    return _assemble_slotted_email(
        slots, ["opening", "pitch", "signoff_name"],
        fallback_subject=f"Quick note for {company}" if company else "Quick note",
        fallback_body=f"Hi,\n\nWanted to reach out - happy to share more if useful.\n\n{signoff}",
    )


def compose_referral_email(client, icp_text, referred_by_company, referred_by_email,
                            new_email):
    """A first-touch email to someone we were just handed off to by an
    existing lead - same slotted format as compose_first_email, but the
    opening names who pointed us here instead of a cold-observation hook."""
    user_prompt = (
        f"RECIPIENT TYPE: specific_person - this is a named individual we were "
        f"just referred to, not a generic inbox.\n\n"
        f"New contact's email address: {new_email}\n\n"
        f"Context: we were talking to {referred_by_company} "
        f"({referred_by_email}), and they told us to reach out to this "
        f"address instead (the right person, a colleague, the decision-maker "
        f"- whatever they said).\n\n"
        f"For the \"opening\" field specifically: warmly name that you got "
        f"their email from {referred_by_email}"
        + (f" at {referred_by_company}" if referred_by_company else "")
        + f", so this reads as a warm handoff, not a cold blast (not the usual "
        f"cold-observation hook - use the referral itself as the opener here). "
        f"For \"pitch\": the short pitch per the usual rules. Don't invent "
        f"details about this new contact or their company that weren't given."
    )
    slots = ask_gemini(client, FIRST_EMAIL_SLOTTED_SYSTEM.format(icp=icp_text, language=EMAIL_LANGUAGE), user_prompt)
    signoff = (slots.get("signoff_name") or "").strip() or "—"
    fallback_subject, fallback_body = (
        ("הכרות קצרה" + (f" — {referred_by_company}" if referred_by_company else ""),
         f"היי,\n\n{referred_by_email} הפנו אותי אליך - אשמח לשתף עוד פרטים אם זה רלוונטי.\n\n{signoff}")
        if EMAIL_LANGUAGE.strip().lower() == "hebrew" else
        ("Quick intro" + (f" — {referred_by_company}" if referred_by_company else ""),
         f"Hi,\n\n{referred_by_email} suggested I reach out - happy to share more if useful.\n\n{signoff}")
    )
    return _assemble_slotted_email(
        slots, ["opening", "pitch", "signoff_name"],
        fallback_subject=fallback_subject,
        fallback_body=fallback_body,
    )


# --------------------------------------------------------------------------
# Strict fixed-template composer for the first cold email. Unlike
# compose_first_email() above (which asks the model to freely write the
# whole opening/pitch), this reads EMAIL_TEMPLATE_FILE - an email YOU write
# almost entirely by hand - and sends it EXACTLY as written, except for
# {{ }} spots:
#   - {{company}} / {{email}} are plain find-and-replace merge fields, no
#     AI involved.
#   - anything else inside {{ }} is a short instruction to the model, and
#     ONLY that spot gets AI-written text - the surrounding fixed text
#     never changes, character for character.
# This is what cmd_run() actually calls for new leads' first email now;
# compose_first_email()/FIRST_EMAIL_SLOTTED_SYSTEM above are kept for
# compose_referral_email() (the warm-handoff email), which still needs to
# adapt more freely per referral.
# --------------------------------------------------------------------------

DEFAULT_EMAIL_TEMPLATE = """\
# This is your first-touch cold email, sent to every new lead almost
# exactly as written below. Lines starting with "#" are comments - they're
# never sent, use them for notes to yourself.
#
# Two kinds of {{ }} spots:
#   {{company}} and {{email}} - swapped for the lead's real company name /
#     email address automatically. No AI, pure find-and-replace.
#   anything else inside {{ }} - a short instruction to the AI for ONLY
#     that one spot. It writes a short piece of text following that
#     instruction and nothing else in the email is touched. Keep each
#     instruction short and specific, and don't put another {{ }} inside
#     one - nesting isn't supported. Instructions can be written in Hebrew
#     or English, doesn't matter - what matters is EMAIL_LANGUAGE in .env
#     (defaults to Hebrew), which is what the filled-in text comes back in.
#
# The first non-comment line MUST start with "SUBJECT:" - everything after
# the blank line under it is the email body, sent as-is. The body below is
# written right-to-left automatically (Gmail RTL rendering kicks in on its
# own once it detects Hebrew), no extra work needed here.

SUBJECT: שאלה קצרה לגבי {{company}}

היי,

{{משפט אחד שמתייחס למשהו קונקרטי ואמיתי על החברה, מתוך המחקר שמופיע למטה - לא מחמאה כללית, ואם אין שום פרט קונקרטי פשוט תשמיטו את המשפט הזה}}

אנחנו עוזרים לצוותי אבטחה שטובעים בשעות של צילומי מצלמות שאין להם זמן לצפות בהן בזמן אמת, לתפוס אוטומטית את מה שבאמת חשוב - במקום לגלות אירוע רק בבדיקה ידנית אחרי המעשה.

{{משפט ישיר אחד שמזמין לתגובה קצרה או לשיחה קצרה - בלי לחץ, בלי דחיפות מזויפת, ובלי "תשמחו לחזור אליי עם כל שאלה"}}

בברכה,
איתי לוי
Lumana / Team 3
"""

_TEMPLATE_SLOT_RE = re.compile(r"\{\{\s*(.+?)\s*\}\}", re.S)
_TEMPLATE_MERGE_FIELDS = {"company", "email"}

FIRST_EMAIL_TEMPLATE_SYSTEM = """You are filling in specific blanks inside an \
otherwise-fixed FIRST-touch cold outreach email, as a real salesperson at the \
company described below. Stay strictly in character, never mention you are \
an AI, and never invent a fact about the recipient/company that isn't given \
to you below.

LANGUAGE: write every blank in {language} - natural, fluent, native-sounding \
{language} that reads as a seamless continuation of the surrounding fixed \
email text, never a stiff word-for-word translation - unless a specific \
blank's own instruction explicitly asks for a different language.

GRAMMAR CHECK (when writing Hebrew): before answering, re-read every blank \
you wrote and fix any grammar mistake, especially these common ones: \
(1) infinitive verbs always keep their "ל" prefix - never write a bare verb \
like "קחת"/"שמוע"/"ראות" where the correct form is "לקחת"/"לשמוע"/"לראות"; \
(2) when one word governs a list of several items with a prefix like \
"ב-"/"ל-"/"מ-" (e.g. "מתעסקים ב-X, Y ו-Z"), that SAME prefix must repeat on \
every item in the list - never drop it or switch to "ה-"/"ו-" partway \
through (e.g. NOT "מתעסקים בייבוא, הטיפול ב-Y" - should be "מתעסקים בייבוא, \
בטיפול ב-Y"); (3) subject-verb number agreement - a plural subject \
("הם"/"הן"/"אתם"/a company referred to in plural) always takes a plural \
verb, e.g. "הם לא מעבירים" never "הם לא מעביר"; (4) grammatical number \
agreement between a numeral and its noun - "מיליוני קילומטרים"/"אלפי \
לקוחות" (plural noun), never "מיליוני קילומטר"/"אלפי לקוח" (singular noun \
after a plural-implying numeral); (5) consistent formality/person across \
ALL blanks in this email - if the email addresses the recipient as \
"אתם"/"לכם" (plural/formal, matching a company), never switch to \
"אתה"/"לך" (singular) in a different blank, and vice versa. Never send a \
sentence with a spelling or grammar error - if you're not sure a sentence \
is fully correct, simplify it rather than risk an error.

HARD RULES for every blank: (1) never use a dash character ("-" or "־") \
anywhere - rephrase instead, e.g. two short sentences instead of one \
joined by a dash; (2) never phrase a blank as a rhetorical question or a \
direct meeting/time request (e.g. "do you have 15 minutes?") unless the \
fixed email text around it is already asking that itself.

--- PERSONA / ICP (for voice/tone only - the offer itself is already fixed \
in the email, don't re-pitch it) ---
{icp}
--- END PERSONA ---

RECIPIENT TYPE: {recipient_note}

Company: {company}
Email address it's going to: {email_addr}

What we found about this company from their own website:
\"\"\"
{research_block}
\"\"\"

You will be given a numbered list of blanks. Each blank sits inside a fixed \
email and is described by one short instruction. Respond with ONLY a single \
JSON object (no markdown fences, no commentary) mapping each blank's key to \
the text that should go there - short, natural prose that reads as a \
seamless part of the surrounding fixed email (matching its capitalization/ \
punctuation context), following that blank's instruction exactly. If an \
instruction says to skip/omit under some condition and that condition \
applies, use an empty string "" for that key rather than inventing filler.

Blanks:
{slots_block}
"""


def parse_email_template(template_text):
    """Strips comment lines, then splits into (subject_template,
    body_template) on the first "SUBJECT:" line. Both may still contain
    {{ }} spots. Raises ValueError if no SUBJECT: line is found."""
    kept_lines = [ln for ln in template_text.splitlines() if not ln.lstrip().startswith("#")]
    text = "\n".join(kept_lines).strip("\n")

    lines = text.split("\n")
    subject_idx = next(
        (i for i, ln in enumerate(lines) if ln.strip().upper().startswith("SUBJECT:")),
        None,
    )
    if subject_idx is None:
        raise ValueError(
            f"{EMAIL_TEMPLATE_FILE} has no line starting with \"SUBJECT:\" - "
            f"add one (see the comments at the top of the default template)."
        )
    subject_template = lines[subject_idx].split(":", 1)[1].strip()
    body_lines = lines[subject_idx + 1:]
    while body_lines and not body_lines[0].strip():
        body_lines = body_lines[1:]
    body_template = "\n".join(body_lines).strip("\n")
    return subject_template, body_template


def _merge_simple_fields(text, company, email_addr, contact_name=""):
    contact_name = (contact_name or "").strip()

    def repl(m):
        key = m.group(1).strip().lower()
        if key == "company":
            return company or ""
        if key == "email":
            return email_addr or ""
        if key == "contact_name":
            return contact_name
        return m.group(0)  # not a merge field - leave for the AI-slot pass
    merged = _TEMPLATE_SLOT_RE.sub(repl, text)
    if not contact_name:
        # {{contact_name}} merged to "" above - without this, a greeting
        # like "Hi {{contact_name}}," turns into "Hi ," (stray space before
        # the comma) whenever a lead has no name on file.
        merged = re.sub(r"[ \t]+,", ",", merged)
    return merged


# --------------------------------------------------------------------------
# Final grammar/style proofread - a dedicated LAST pass run on every
# composed first-touch email (company_emails' template fill AND
# personal_emails' persona fill), on top of (not instead of) each
# composer's own "GRAMMAR CHECK" instructions above. Those instructions ask
# the SAME call that's busy inventing the pitch to also proofread itself,
# which is exactly the kind of thing a model skips under load - a second,
# narrowly-scoped call whose ONLY job is proofreading catches what the
# first call missed (dropped prefixes, verb agreement, a stray dash) far
# more reliably than asking one call to do both jobs at once.
# --------------------------------------------------------------------------

PROOFREAD_SYSTEM = """You are a strict copy editor proofreading a cold \
outreach email that's about to be sent. You did NOT write it and you are \
NOT rewriting the pitch, the tone, or the structure - your ONLY job is to \
catch and fix language mistakes.

Check the subject and body below for, in {language}:
1) Grammar mistakes - especially: (a) an infinitive verb missing its "ל" \
prefix (e.g. "קחת"/"שמוע"/"ראות" instead of "לקחת"/"לשמוע"/"לראות"); \
(b) a preposition prefix ("ב-"/"ל-"/"מ-") that governs a list of several \
items but isn't repeated on every item in that list (e.g. "מתעסקים \
בייבוא, הטיפול ב-Y" instead of "מתעסקים בייבוא, בטיפול ב-Y"); (c) a plural \
subject ("הם"/"הן"/"אתם"/a company referred to in plural) paired with a \
singular verb; (d) a plural-implying numeral paired with a singular noun \
(e.g. "מיליוני קילומטר" instead of "מיליוני קילומטרים"); (e) inconsistent \
formality/person across the email (mixing "אתם"/"לכם" with "אתה"/"לך" for \
the same recipient).
2) Any spelling mistake or typo.
3) A literal dash character (- or ־) anywhere at all - never allowed, no \
exceptions.
4) Any other sentence a native {language} speaker would find awkward, \
unnatural, or ungrammatical.

If you find ANY of the above, rewrite ONLY the broken sentence(s) to fix \
them - keep every other word, the tone, the structure, and the meaning \
EXACTLY as given. Never add a new sentence, never remove a sentence, never \
change the pitch or the ask. If a dash needs removing, rephrase around it \
(e.g. split into two short sentences) rather than just deleting the \
character and leaving a run-on.

If the email is already completely clean, return it unchanged.

Respond with ONLY a single JSON object (no markdown fences, no commentary):
  "had_issues": true if you changed anything, false if it was already clean
  "subject": the (possibly corrected) subject line
  "body": the (possibly corrected) body

Subject: {subject}
Body:
\"\"\"
{body}
\"\"\"
"""


def proofread_email(client, subject, body, language=None, label=""):
    """Last-line grammar/style backstop for a composed first-touch email -
    see the block comment above. Best-effort: if the proofread call itself
    fails, or its own answer is empty, or its "fix" introduces a NEW hard
    style violation (most importantly a dash, which this pass is
    explicitly told to never introduce) that the original didn't have,
    this silently keeps the original subject/body rather than risking
    making things worse - a proofreading pass is a safety net, never
    something that can make an otherwise-fine email crash or come back
    empty."""
    subject = subject or ""
    body = body or ""
    if not body.strip():
        return subject, body
    language = (language or EMAIL_LANGUAGE or "Hebrew").strip()
    try:
        result = ask_gemini(
            client,
            PROOFREAD_SYSTEM.format(language=language, subject=subject, body=body),
            "Proofread now, as the JSON object described above.",
        )
    except Exception as e:
        print(f"  proofread pass failed for {label or 'this email'} ({e}) - "
              f"keeping the original draft.")
        return subject, body

    fixed_subject = (result.get("subject") or "").strip()
    fixed_body = (result.get("body") or "").strip()
    if not fixed_subject or not fixed_body:
        return subject, body

    # A proofread that introduces a violation the original didn't have
    # (almost always a dash it failed to rephrase around) is worse than no
    # proofread at all - never trade a grammar fix for a new hard-rule
    # break.
    new_violation = find_style_violation(fixed_subject, fixed_body)
    if new_violation and not find_style_violation(subject, body):
        print(f"  proofread pass for {label or 'this email'} introduced a new "
              f"violation ({new_violation}) - keeping the original draft instead.")
        return subject, body

    if result.get("had_issues"):
        print(f"  proofread pass fixed a grammar/style issue in {label or 'this email'}.")
    return fixed_subject, fixed_body


def compose_first_email_from_template(client, icp_text, template_text, company, email_addr,
                                       research="", recipient_kind="specific_person"):
    subject_template, body_template = parse_email_template(template_text)
    subject_merged = _merge_simple_fields(subject_template, company, email_addr)
    body_merged = _merge_simple_fields(body_template, company, email_addr)

    combined = f"{subject_merged}\n\x00\n{body_merged}"  # \x00: cheap subject/body separator
    instructions = [m.group(1).strip() for m in _TEMPLATE_SLOT_RE.finditer(combined)]

    def _fill_blanks(filled, instr_by_key, slot_keys):
        counter = {"i": 0}

        def fill_repl(m):
            key = slot_keys[counter["i"]]
            counter["i"] += 1
            text = (filled.get(key) or "").strip()
            hit = find_banned_phrase(text)
            if hit:
                print(f"  WARNING: a template blank contained a banned phrase ({hit!r}) - "
                      f"leaving that blank empty instead of sending it.")
                return ""
            carrier_hit = find_carrier_violation(instr_by_key.get(key, ""), text)
            if carrier_hit:
                print(f"  WARNING: a template blank {carrier_hit} - leaving that blank "
                      f"empty instead of sending it.")
                return ""
            return text

        combined_filled = _TEMPLATE_SLOT_RE.sub(fill_repl, combined)
        subj, _, bod = combined_filled.partition("\n\x00\n")
        bod = re.sub(r"\n[ \t]*\n[ \t]*\n+", "\n\n", bod).strip()
        return subj, bod

    if not instructions:
        subject, body = subject_merged, body_merged
    else:
        research_block = research.strip() if research and research.strip() else (
            "(no research available for this one - don't invent company specifics)"
        )
        recipient_note = (
            "generic_inbox - this looks like a shared/role inbox (info@, support@, "
            "office@, etc.), not a specific named person."
            if recipient_kind == "generic_inbox" else
            "specific_person - this looks like it belongs to a named individual."
        )
        slot_keys = [f"slot_{i+1}" for i in range(len(instructions))]
        instr_by_key = dict(zip(slot_keys, instructions))
        slots_block = "\n".join(f"  {k}: {instr}" for k, instr in zip(slot_keys, instructions))
        system_prompt = FIRST_EMAIL_TEMPLATE_SYSTEM.format(
            icp=icp_text, language=EMAIL_LANGUAGE, recipient_note=recipient_note,
            company=company, email_addr=email_addr, research_block=research_block,
            slots_block=slots_block,
        )
        filled = ask_gemini(client, system_prompt, "Fill in the blanks now, as the JSON object described above.")
        subject, body = _fill_blanks(filled, instr_by_key, slot_keys)

        # Code-level backstop for the HARD RULES in FIRST_EMAIL_TEMPLATE_SYSTEM
        # (dashes, rhetorical time-asks) - same one-retry pattern
        # compose_person_email() uses below: tell the model exactly what it
        # got wrong and ask it to redo just the blanks, before falling back
        # to the safe generic email.
        violation = find_style_violation(subject, body)
        if violation:
            print(f"  company email for {company or email_addr} violated a style rule "
                  f"({violation}) - asking the model to redo the blanks once.")
            retry_filled = ask_gemini(
                client, system_prompt,
                f"Your previous answer's assembled email violated a HARD RULE: "
                f"{violation}. Redo it: rewrite the blanks so this stops being true, "
                f"keeping everything else about your approach the same. Respond with "
                f"ONLY the JSON object described above.",
            )
            retry_subject, retry_body = _fill_blanks(retry_filled, instr_by_key, slot_keys)
            if not find_banned_phrase(retry_subject, retry_body) and \
               not find_style_violation(retry_subject, retry_body):
                subject, body = retry_subject, retry_body
            else:
                print(f"  retry still violated a style rule for {company or email_addr} - "
                      f"using a safe fallback instead.")
                subject, body = None, None

    if subject is None or body is None or find_banned_phrase(subject, body):
        if subject is not None:
            print(f"  WARNING: the assembled email still contained a banned phrase "
                  f"after per-blank checking - using a safe fallback instead.")
        if EMAIL_LANGUAGE.strip().lower() == "hebrew":
            subject = f"פנייה קצרה ל{company}" if company else "פנייה קצרה"
            body = "היי,\n\nרציתי ליצור קשר ואשמח לשתף עוד פרטים אם זה רלוונטי.\n\nבברכה,\nאיתי לוי"
        else:
            subject = f"Quick note for {company}" if company else "Quick note"
            body = "Hi,\n\nWanted to reach out, happy to share more if useful.\n\nBest,\nItay Levi"
        return {"subject": subject, "body": body}

    subject, body = proofread_email(client, subject, body, label=company or email_addr)
    return {"subject": subject, "body": body}


# --------------------------------------------------------------------------
# Persona-specific first emails (personal_emails table ONLY): several fixed
# templates (personal_email_templete.txt), one per kind of recipient (job title /
# industry). The model picks whichever one best fits THIS person's job
# title and their linkedin_info, then fills that template's {{ }} blanks -
# same strict-template rules as compose_first_email_from_template() above,
# just with a template-picking step folded into the same call instead of a
# separate one, so this costs no extra API calls over a plain
# single-template path.
#
# This is the ONLY composer personal_emails rows use - there is no
# fallback to EMAIL_TEMPLATE_FILE any more (that's company_emails' composer
# now - see compose_first_email_from_template() above, called directly for
# those rows in _run_once()). If personal_email_templete.txt is empty, or the model can't
# confidently match this person to any persona, compose_person_email()
# below returns None instead of guessing - _run_once() flags that person
# for your attention rather than sending a generic/mismatched email to a
# named individual.
# --------------------------------------------------------------------------

DEFAULT_PERSONA_LABEL = "ברירת מחדל / לא מתאים לאף פרסונה"


def _load_personas():
    """Parses PERSONA_TEMPLATES_FILE into {label: (subject_template,
    body_template)}. Sections are separated by a line containing only
    "---"; within a section, the first line is the label (must end with
    ":"), everything after it is fed straight through parse_email_template()
    - same "SUBJECT:" line + {{ }} rules as EMAIL_TEMPLATE_FILE. Returns {}
    if the file doesn't exist (feature not set up) - a malformed individual
    section is skipped with a warning rather than crashing the whole run."""
    if not PERSONA_TEMPLATES_FILE.exists():
        return {}
    text = PERSONA_TEMPLATES_FILE.read_text(encoding="utf-8")
    personas = {}
    for block in text.split("\n---\n"):
        # drop comment lines (and any blank lines left behind) from the
        # START of the block only - a block can be "just comments" (e.g.
        # the instructions at the top of the file, with no persona in it
        # at all), or a real persona whose label line simply isn't first
        # because someone left themselves a note above it.
        lines = block.strip("\n").splitlines()
        while lines and (not lines[0].strip() or lines[0].lstrip().startswith("#")):
            lines = lines[1:]
        if not lines:
            continue
        header = lines[0].strip()
        if not header.endswith(":"):
            continue  # malformed - no label line found, skip it
        label = header[:-1].strip()
        body_text = "\n".join(lines[1:]).strip()
        try:
            personas[label] = parse_email_template(body_text)
        except Exception as e:
            print(f"  Warning: couldn't parse the '{label}' persona in "
                  f"{PERSONA_TEMPLATES_FILE.name}: {e} - skipping it this run.")
    return personas


# --------------------------------------------------------------------------
# Role classification (personal_emails only) - "Spikee" role-routing spec.
# Every named contact's job_title gets classified into one of 5 categories
# BEFORE compose_person_email() ever runs on it (see classify_role() below,
# called from the backfill pass and the new-leads loop in _run_once()).
# This is a SEPARATE axis from personal_email_templete.txt's own persona
# matching (which is about the recipient's INDUSTRY/site-type - hotel vs
# factory vs security site - and picks WHICH capability to pitch); role
# classification is about the recipient's SENIORITY/FUNCTION and drives two
# things: (a) whether HR gets routed away from a direct pitch entirely (see
# find_better_contact_at_company()/compose_hr_referral_email() below), and
# (b) how long/terse the email should be for a CEO/founder who has no time
# to read a normal-length pitch (see ROLE_STYLE_NOTES + compose_person_email()'s
# short_body handling).
# --------------------------------------------------------------------------

ROLE_KEYWORDS = {
    "founder": [
        "Founder", "Co-Founder", "Owner", "יזם", "יזמת", "מייסד", "מייסדת",
        "בעל עסק", "בעלת עסק", "בעל/ת עסק",
        # "בעלים" (plain "owner(s)") - the standard word you'll actually see
        # on a company registry/Excel export, much more common in practice
        # than "בעל עסק" above. Added after a real batch of imported leads
        # showed a bare "בעלים" falling through to the AI fallback (and, in
        # some rows, past _could_match_any_persona's hints too) instead of
        # matching for free.
        "בעלים",
        # "שותף מנהל" (Managing Partner) - a real title from a leads export;
        # treated as founder-equivalent (personal financial stake, not a
        # hired exec) rather than ceo, matching the founder short_body
        # style's assumption that both the operational and financial side
        # matter personally to this reader.
        "שותף מנהל",
    ],
    "ceo": [
        "CEO", "Managing Director", "General Manager", 'מנכ"ל', "מנכ״ל",
        "מנהל כללי", "מנהלת כללית", 'בעלים ומנכ"ל', "בעלים ומנכ״ל",
        # "נשיא" (President) - CEO-equivalent title, wasn't matched anywhere.
        "נשיא",
        # Chairman of the Board ("יו"ר"/"יו״ר", standalone or with
        # "הדירקטוריון"/"מועצת המנהלים" after it) and bare board-member
        # titles ("דירקטור", "ח. דירקטוריון") - per an explicit decision to
        # treat ANY board-level title as a decision-maker (not everyone
        # would route both the same way - a non-exec board member often
        # has no say in day-to-day purchasing - but this is the choice that
        # was made for this list).
        'יו"ר', "יו״ר", "דירקטור",
    ],
    "security": [
        "Security Manager", "CSO", "Head of Security", "Head of Physical Security",
        "Physical Security Manager", "VP Security", "Director of Security", 'קב"ט', "קב״ט",
        "מנהל אבטחה", "מנהלת אבטחה", "ראש אגף ביטחון", "ראש אגף בטחון",
    ],
    "ops_coo": [
        "COO", "VP Operations", "Head of Operations", 'סמנכ"ל תפעול',
        "סמנכ״ל תפעול", "מנהל תפעול", "מנהלת תפעול", "ראש חטיבת תפעול",
        # "אופרציה" - the Hebraized-English spelling of "operations" that
        # shows up just as often in real titles as the formal "תפעול".
        "אופרציה",
    ],
    "hr": [
        "HR Manager", "VP People", "Talent Acquisition Manager",
        "מנהל משאבי אנוש", "מנהלת משאבי אנוש", "מנהל גיוס", "מנהלת גיוס",
        "מנהל/ת גיוס", "רכז גיוס", "רכזת גיוס", "רכז/ת גיוס",
        # Bare "משאבי אנוש"/"כוח אדם" (no "מנהל/ת" prefix) - added because a
        # real title like "סמנכ"ל משאבי אנוש" (VP HR) only contains the
        # bare phrase, not the "מנהל משאבי אנוש" one above (the "סמנכ"ל"
        # prefix replaces "מנהל/ת" entirely) - it was falling through to
        # the AI fallback for something the keyword list should catch for
        # free every time, since correctly routing HR away from a direct
        # pitch is exactly the kind of decision you don't want resting on
        # an AI call succeeding.
        "משאבי אנוש", "כוח אדם",
        # "כח-אדם"/"כח אדם" - same word, spelled without the vav (a very
        # common informal spelling) and sometimes hyphenated. A different
        # string from "כוח אדם" above, so it needs its own entry.
        "כח-אדם", "כח אדם",
    ],
}

# HR is deliberately never a pitch target (see the routing logic below) -
# these are the categories a same-company contact needs to already be
# classified as for find_better_contact_at_company() to prefer them, in
# this priority order.
_HR_ENRICH_PRIORITY = ["security", "ops_coo", "ceo"]


# Real-world job-title exports are full of abbreviated forms of "מנהל/ת"
# (manager) - "מ. אחזקה", "מ׳ רכש", "מנ' תפעול" etc. - that never match any
# keyword above no matter how complete the keyword list is, because the
# list is written in full words. This expands the abbreviation to the full
# word BEFORE matching, so e.g. "מ. תפעול" is treated exactly like "מנהל
# תפעול" (an existing ops_coo keyword) instead of silently matching
# nothing. Deliberately only expands "מנהל/ת" - it does NOT decide what
# "מ. אחזקה" (maintenance) means, since "אחזקה" still isn't a keyword
# anywhere; that one still correctly falls through to needs_attention.
_TITLE_ABBREV_RE = re.compile(r'(?<![א-ת])מ[\.׳’\']\s*')


def _normalize_title_abbreviations(title):
    """See _TITLE_ABBREV_RE above. Expands a leading/embedded "מ." or "מ'"
    abbreviation to "מנהל " - safe to run before every keyword/hint check
    in this file, never changes a title that has no such abbreviation."""
    return _TITLE_ABBREV_RE.sub("מנהל ", title or "")


def _keyword_role_match(raw_title):
    """Cheap, free, deterministic first pass - no API call. Returns
    (category, confidence) for the single best-scoring keyword hit across
    every category, or (None, 0.0) if nothing matched at all. Confidence is
    a simple heuristic (exact title match > whole-word match within a
    longer title > bare substring match), not a calibrated probability -
    good enough to separate "obviously this" from "needs the AI fallback"."""
    title = _normalize_title_abbreviations((raw_title or "").strip())
    if not title:
        return None, 0.0
    title_lower = title.lower()
    best_category, best_score = None, 0.0
    for category, keywords in ROLE_KEYWORDS.items():
        for kw in keywords:
            kw_lower = kw.lower()
            if not kw_lower:
                continue
            if title_lower == kw_lower:
                score = 0.97
            elif re.search(rf"(?<!\S){re.escape(kw_lower)}(?!\S)", title_lower):
                score = 0.90
            # Bare substring match - but NOT if a Hebrew letter sits directly
            # against either end of the match, since that means the keyword
            # is only there as part of a LONGER Hebrew word, not standing on
            # its own. Without this, "סמנכ\"ל משאבי אנוש" (VP/deputy of HR)
            # was matching the "ceo" keyword 'מנכ"ל' as a bare substring -
            # סמנכ"ל literally contains those letters - misclassifying every
            # סמנכ"ל-anything (VP Marketing, CFO, VP Business Dev, ...) as
            # CEO. A punctuation/quote-style difference (e.g. " vs ״) still
            # matches fine here since those aren't Hebrew letters.
            elif re.search(rf"(?<![א-ת]){re.escape(kw_lower)}(?![א-ת])", title_lower):
                score = 0.78
            else:
                continue
            if score > best_score:
                best_category, best_score = category, score
    return best_category, best_score


# --------------------------------------------------------------------------
# "Do I even have a persona template for this person?" pre-check - run
# BEFORE ever scraping a personal_emails lead's website or calling the AI
# to compose an email for them (see _run_once() below). Deliberately a
# SEPARATE keyword list from ROLE_KEYWORDS above: ROLE_KEYWORDS drives role
# CLASSIFICATION (HR-routing, CEO/founder short_body - a different axis,
# see the big comment above ROLE_KEYWORDS), while this one only asks
# whether personal_email_templete.txt's personas - matched on job_title AND
# industry together, not on job_title alone - could plausibly cover this
# lead at all. Kept deliberately loose: a false "yes" costs one scrape/
# compose call that ends up needs_attention anyway; a false "no" means
# skipping a lead's whole outreach before ever giving it a real chance.
# --------------------------------------------------------------------------

_PERSONA_TITLE_HINTS = [
    # מנהלי ביטחון וקב"טים
    'קב"ט', "קב״ט", "מנהל אבטחה", "מנהלת אבטחה", "ראש אגף ביטחון", "ראש אגף בטחון",
    "security", "cso",
    # סמנכ"לי ומנהלי משאבי אנוש / כוח אדם - "כח אדם"/"כח-אדם" (no vav) is
    # the same word as "כוח אדם" in a different common spelling.
    "משאבי אנוש", "כוח אדם", "כח אדם", "כח-אדם", "גיוס", "human resources",
    "talent acquisition",
    # סמנכ"לי תפעול במפעלים - "אופרציה" is the Hebraized-English spelling
    # of "operations" seen just as often as the formal "תפעול".
    'סמנכ"ל תפעול', "סמנכ״ל תפעול", "מנהל תפעול", "מנהלת תפעול", "operations",
    "אופרציה",
    # מלונאות, מנכ"לים וסמנכ"לי תפעול + the general owner/CEO/VP catch-all -
    # deliberately broad: any owner/CEO/VP-shaped title at all, any industry.
    # "נשיא"/"יו"ר"/"שותף"/"דירקטור" (President/Chairman/Partner/Director)
    # added alongside the existing English president/chairman/chairwoman -
    # real titles from a leads export that weren't hinted at all before.
    # IMPORTANT: any category added to ROLE_KEYWORDS above needs its own
    # matching entry here too (or the closest available stem) - this check
    # runs BEFORE role classification even gets a chance to, so a title
    # that keyword-matches a role but has no hint here gets dropped as
    # "no persona could plausibly match" without role classification ever
    # running at all.
    "מנכ", "בעל", "סמנכ", "founder", "owner", "ceo", "managing director",
    "general manager", "president", "chairman", "chairwoman",
    "נשיא", 'יו"ר', "יו״ר", "שותף", "דירקטור",
]

# Loose hints for the INDUSTRY column - consulted only when job_title alone
# gave no hint above (e.g. a bare "מנהל"/"מנהלת" title): if the industry on
# file is one of the three site-types with their OWN dedicated persona
# (hospitality/factory/security site), that's still worth a shot even
# though the title itself was inconclusive. Never used to say "no" by
# itself - only ever makes this check MORE lenient, never less.
_INDUSTRY_HINT_KEYWORDS = [
    # מלון (hotel, ends in final-nun ן) and מלונא- (מלונאות/hospitality,
    # regular נ before the suffix) are two different words in Hebrew - both
    # listed separately rather than relying on one being a substring of
    # the other (same final-letter gotcha the ROLE_KEYWORDS matcher above
    # has hit before).
    "מלון", "מלונא", "אירוח", "hotel", "hospitality",
    "מפעל", "ייצור", "תעשי", "factory", "industrial", "manufactur",
    "אבטחה", "בטחון", "ביטחון", "security",
]


def _could_match_any_persona(job_title, industry=""):
    """See the block comment above. Checks job_title first (cheap, no API
    call); only if that's inconclusive does it give the industry column a
    loose second look. Returns True whenever there's a real chance, False
    only when neither column gives any reason to think a persona applies.

    This is the harder of the two gates in this file (see
    _keyword_role_match above) - there's no AI fallback here at all, so a
    title's abbreviation gets normalized the same way before checking,
    otherwise something like "מ. תפעול" would be silently dropped even
    though "מנהל תפעול" (what it actually means) is a known hint below."""
    title = _normalize_title_abbreviations((job_title or "").strip()).lower()
    if title and any(hint.lower() in title for hint in _PERSONA_TITLE_HINTS):
        return True
    industry_text = (industry or "").strip().lower()
    if industry_text and any(kw.lower() in industry_text for kw in _INDUSTRY_HINT_KEYWORDS):
        return True
    return False


_ROLE_CATEGORY_LABELS = {
    "founder": "יזם/ת ומייסד/ת (Founder)",
    "ceo": 'מנכ"ל (CEO / Managing Director)',
    "security": 'קב"ט / מנהל/ת אבטחה (Security Manager / CSO)',
    "ops_coo": 'סמנכ"ל תפעול (COO / VP Operations)',
    "hr": "מנהל/ת משאבי אנוש (HR Manager / VP People)",
}

ROLE_CLASSIFY_AI_SYSTEM = """Classify a single job title into EXACTLY ONE of \
these 5 categories, or "other" if none genuinely fit:

""" + "\n".join(f'- "{k}": {v}' for k, v in _ROLE_CATEGORY_LABELS.items()) + """
- "other": doesn't clearly fit any category above (a different function \
entirely, or too vague to tell - e.g. "מנהל אזור צפון" with no functional \
area named).

Respond with ONLY a single JSON object: {"category": one of the 5 keys \
above or "other", "confidence": your confidence in this pick, 0.0-1.0 - be \
honest and conservative, a vague/ambiguous title should score LOW (under \
0.5), not force a confident-sounding guess}.

Job title: "{{RAW_TITLE}}"
"""


def classify_role_ai(client, raw_title):
    """AI fallback for a title the keyword pass wasn't confident about - one
    call, no free text. Returns (category, confidence); ("other", 0.0) if
    the model's answer doesn't parse into something usable (never raises -
    a classification hiccup should never be able to crash a run, same
    philosophy as everything else network/API-dependent in this file)."""
    try:
        result = ask_gemini(
            client, ROLE_CLASSIFY_AI_SYSTEM.replace("{{RAW_TITLE}}", raw_title),
            "Classify it now, as the JSON object described above.",
        )
        category = (result.get("category") or "").strip().lower()
        confidence = float(result.get("confidence") or 0.0)
    except Exception as e:
        print(f"  role classification AI call failed for {raw_title!r}: {e} "
              f"- treating as unclassified (needs_review) instead.")
        return "other", 0.0
    if category not in ROLE_KEYWORDS:
        category = "other"
    return category, max(0.0, min(1.0, confidence))


def classify_role(client, raw_title):
    """The single entry point _run_once() (and its backfill pass) calls for
    every personal_emails contact. Two-step: a free keyword match first
    (covers the large majority of real-world titles), an AI call only when
    that wasn't confident enough. Returns a dict with "category" (one of
    ROLE_KEYWORDS' keys, or "other" when nothing fits - the classifier's
    raw best guess, ALWAYS - see effective_role_category() below for the
    separate, threshold-gated decision of whether that guess is confident
    enough to actually route/behave on), "confidence", and "method"
    ("keyword"/"ai"/"none" for a blank title). Keeping the raw guess here
    (rather than collapsing a low-confidence one to "needs_review" inside
    this function) is deliberate: it's what gets stored in Supabase, so you
    can actually see what the classifier THOUGHT even when it wasn't
    confident enough to be trusted - which is exactly what you need to
    judge whether ROLE_NEEDS_REVIEW_THRESHOLD is set right, per the spec's
    own open question about tuning it from real runs."""
    title = (raw_title or "").strip()
    if not title:
        return {"category": "other", "confidence": 0.0, "method": "none"}

    category, score = _keyword_role_match(title)
    if category and score >= ROLE_KEYWORD_ACCEPT_THRESHOLD:
        return {"category": category, "confidence": round(score, 2), "method": "keyword"}

    ai_category, ai_confidence = classify_role_ai(client, title)
    final_category = ai_category if ai_category in ROLE_KEYWORDS else "other"
    return {"category": final_category, "confidence": round(ai_confidence, 2), "method": "ai"}


def effective_role_category(category, confidence):
    """The threshold gate (page 7's route_contact() pseudocode: "if
    role.confidence < 0.6: mark_needs_review(...), continue with the
    default"). Separate from classify_role() itself so the RAW guess still
    gets stored (see classify_role()'s docstring) while every actual
    behavior decision (HR routing, CEO/founder short_body) is only ever
    made on a confident-enough classification - anything under
    ROLE_NEEDS_REVIEW_THRESHOLD is treated as "needs_review" here, which
    compose_person_email()/the HR-routing check both already handle as
    "no special behavior, exactly as before this feature existed"."""
    try:
        confidence = float(confidence or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence < ROLE_NEEDS_REVIEW_THRESHOLD:
        return "needs_review"
    return (category or "other").strip().lower()


# --------------------------------------------------------------------------
# Batch persona + role classification - ONE Gemini call for an entire batch
# of personal_emails leads, instead of a separate AI decision per lead.
#
# Before this existed, a run with N new personal_emails leads could spend
# up to N calls on classify_role_ai() (the keyword-match fallback) PLUS
# another N calls buried inside compose_person_email() itself (which used
# to pick a persona freely, as part of the same call that filled in the
# blanks) - and a lead that failed that pick still burned the research +
# compose (+ retry, + proofread) calls before ending up "needs_attention"
# anyway. That's what was driving the call count sky-high on a run with a
# lot of new leads (e.g. a batch of hotel-industry CEOs that all failed to
# match a persona at compose time, each after 2-3 wasted calls).
#
# Now: classify_role()'s cheap keyword pass still runs first and for free
# (unchanged) - this function is only for whatever's left after that. Every
# survivor of the free _could_match_any_persona() gate goes into ONE
# request that returns, per lead, both "persona" (which
# personal_email_templete.txt template - if any - actually fits) and
# "role_category" (for HR-routing/short_body). Only leads that come back
# with a real persona match go on to a liveness check and a compose call at
# all - see _run_once()'s personal_emails handling.
# --------------------------------------------------------------------------

PERSONA_BATCH_SYSTEM = """You will be given a BATCH of sales leads (job \
title, industry, contact name, company) and a NUMBERED list of persona \
labels, each one written for a specific kind of recipient. For EACH lead \
in the batch, independently decide:

1) "persona_id": the NUMBER (from the numbered list below) of the SINGLE \
persona that genuinely fits this person best, or 0 if none of them \
genuinely fit. Answer with JUST the number - never the label text itself. \
Don't force a bad match just to give an answer - a wrong persona pick is \
worse than admitting none fit. Use the job title together with the \
industry and company name (and contact name, if it hints at anything) to \
judge which persona's own audience this lead actually belongs to - and \
also use "linkedin_info"/"company_research" whenever they're not "(none \
on file)": a bare title like "COO"/"CEO"/"Owner" often isn't enough on its \
own to tell industries apart (a hotel's CEO and a factory's CEO need \
different personas even though their title is the same word), so lean on \
whatever research is given about the person or their company to decide \
which industry-specific persona actually applies, exactly the way a human \
salesperson would.

COMPANY-FIT GATE (check this BEFORE picking a persona, using the ICP below): \
a persona label matching this person's JOB TITLE is not enough on its own - \
the COMPANY they work at also has to be a plausible fit for what we \
actually sell (see "WHO WE SELL TO" below). Someone whose title sounds like \
"Operations Manager"/"COO"/"Facilities Manager" still gets persona_id 0 if \
their company's own business (industry/company_research/company name) \
doesn't plausibly involve the kind of multi-site camera/security \
deployment the ICP describes - e.g. a travel agency, a law firm, an \
accounting firm, a software/SaaS company, a marketing agency, a small \
office with no physical site to speak of. When industry/company_research \
gives no clear signal either way, judge from the company name and job \
title together the way a real salesperson qualifying a cold list would - \
if it's genuinely unclear whether this company even has the kind of \
camera-heavy physical site the ICP targets, treat that uncertainty as a \
reason to return persona_id 0, not as a coin flip in favor of a match. \
Conversely, never reject a lead JUST because its exact vertical isn't \
spelled out word for word in the ICP prose below - that text is \
illustrative, not an exhaustive checklist. THE PERSONA LABELS THEMSELVES \
ARE PART OF THE ICP, and take priority over the prose when the two seem to \
disagree: if a persona label below is written for a specific kind of \
company (e.g. a hospitality/hotel persona, a factory/industrial persona, a \
warehouse/logistics persona) or explicitly says it applies "across any \
industry" (like a generic owner/CEO or HR/workforce persona), a lead whose \
company genuinely matches that description is IN scope for this gate - \
full stop - even if that exact word never appears in the ICP prose. The \
ICP prose is there to describe the general shape of who we sell to; the \
persona list is the concrete, authoritative answer for which specific \
verticals and functions we already pitch. Reserve persona_id 0 from this \
gate for companies that don't fit ANY persona's described audience, not \
for companies that fit a persona fine but happen to use different wording \
than the ICP summary above it.

2) "role_category": classify this same lead's SENIORITY/FUNCTION into \
EXACTLY ONE of these categories (a completely separate question from #1 - \
this is about how senior/what function they are, not which industry \
pitch fits them, and NOT affected by the company-fit gate above - a \
COO at an off-ICP company is still a "COO" for this field, even though \
persona_id is 0 for them):

""" + "\n".join(f'- "{k}": {v}' for k, v in _ROLE_CATEGORY_LABELS.items()) + """
- "other": doesn't clearly fit any category above.

3) "confidence": your confidence in the "role_category" pick (#2 only), \
0.0-1.0 - be honest and conservative, a vague/ambiguous title should score \
LOW (under 0.5).

IMPORTANT: keep every answer as SHORT as possible - a numeric "persona_id", \
a short "role_category" word, a number for "confidence" - this batch has \
{lead_count} leads in it and a long answer per lead risks your response \
getting cut off before it's done, which would throw away the WHOLE batch's \
answer, not just one lead's. Never repeat a persona's label text back in \
your answer - the number is enough.

--- WHO WE SELL TO (ICP - use this for the company-fit gate above; ignore \
anything about tone/voice/writing style here, that's not relevant to this \
task) ---
{icp}
--- END WHO WE SELL TO ---

--- PERSONA LABELS (numbered - answer with a persona_id NUMBER, or 0 for none) ---
{persona_labels_block}
--- END PERSONA LABELS ---

--- LEADS (a JSON array, in no particular order) ---
{leads_json}
--- END LEADS ---

Respond with ONLY a single JSON object (no markdown fences, no commentary) \
of this exact shape:
  "results": a JSON array with ONE object per lead above, IN THE SAME ORDER \
they were given, each with EXACTLY these keys: "key" (copy that lead's own \
"key" value character for character, so the answer can be matched back up \
to the right lead), "persona_id", "role_category", "confidence".
"""


def classify_personas_batch(client, personas, candidates, icp_text=""):
    """ONE Gemini call that classifies an ENTIRE batch of personal_emails
    leads at once (see the block comment above). `candidates` is a list of
    dicts, each with "key" (anything unique you can match the result back
    up by - the row's email is a good choice), "job_title", "industry",
    "contact_name", "company". `icp_text` is icp.txt's contents (see
    build_persona_text()/_run_once()) - passed through to the model so it
    can gate on whether the LEAD'S COMPANY is even a plausible fit for what
    we sell, not just whether their job title matches a persona's audience
    (see the COMPANY-FIT GATE in PERSONA_BATCH_SYSTEM above - this is what
    keeps something like a travel agency's ops manager from getting
    matched to the generic ops/facilities persona just because the title
    superficially fits). Optional/defaults to "" for backward compatibility
    with any caller that doesn't have it handy - the company-fit gate is
    simply weaker (job-title-only) in that case, same as before this
    parameter existed.

    Returns a dict {key: {"persona": str or None, "role_category": str,
    "confidence": float}}. A candidate the model's answer doesn't cover (or
    answers in a way that doesn't parse) defaults to {"persona": None,
    "role_category": "other", "confidence": 0.0} - same fail-safe
    philosophy as classify_role_ai(): a batch-classification hiccup should
    flag things for a human to review, never crash the run or silently
    invent a match. Never raises."""
    fallback = {c["key"]: {"persona": None, "role_category": "other", "confidence": 0.0}
                for c in candidates}
    if not candidates:
        return {}
    if not personas:
        print(f"  batch persona/role classification: 0 persona templates were loaded "
              f"(see the warning near the start of this run) - flagging all "
              f"{len(candidates)} lead(s) for review instead of guessing.")
        return fallback

    # Personas are referenced by NUMBER in the prompt/response, never by
    # their (often long, e.g. a full sentence in Hebrew) label text - with
    # a big batch, having the model repeat every label back verbatim for
    # EVERY lead was by far the biggest driver of output length, and a
    # response that runs out of room mid-generation comes back as truncated,
    # invalid JSON - which used to throw away the WHOLE batch's answer, not
    # just the one lead that happened to be mid-object when it got cut off.
    # persona_by_id[0] is deliberately unused - 0 means "no fitting role".
    persona_ids = {i + 1: label for i, label in enumerate(personas.keys())}
    persona_labels_block = "\n".join(f"{i}. {label}" for i, label in persona_ids.items())
    leads_payload = [
        {
            "key": c["key"],
            "job_title": (c.get("job_title") or "").strip() or "(not given)",
            "industry": (c.get("industry") or "").strip() or "(not given)",
            "contact_name": (c.get("contact_name") or "").strip() or "(not given)",
            "company": (c.get("company") or "").strip() or "(not given)",
            # Optional - only present when already sitting on the row for
            # free (see the caller). Truncated defensively so one lead with
            # an unusually long cached research blob can't blow up the
            # whole batch prompt's size.
            "linkedin_info": (c.get("linkedin_info") or "").strip()[:1500] or "(none on file)",
            "company_research": (c.get("company_research") or "").strip()[:1500] or "(none on file)",
        }
        for c in candidates
    ]
    system_prompt = PERSONA_BATCH_SYSTEM.format(
        persona_labels_block=persona_labels_block,
        leads_json=json.dumps(leads_payload, ensure_ascii=False, indent=2),
        lead_count=len(candidates),
        icp=icp_text.strip() if icp_text and icp_text.strip() else
            "(no ICP text loaded - fall back to judging fit from the job title/industry/company alone)",
    )
    try:
        result = ask_gemini(
            client, system_prompt,
            "Classify every lead in the batch now, as the JSON object described above.",
        )
        items = result.get("results") if isinstance(result, dict) else None
        if not isinstance(items, list):
            raise ValueError("response wasn't the expected {\"results\": [...]} shape")
    except Exception as e:
        # A single big batch call failing (usually a truncated/invalid JSON
        # response - see the module comment above) used to mean every lead
        # in it got flagged for review, even though most of them probably
        # would have classified fine in a smaller call. Bisect once instead:
        # split the batch in half and retry each half on its own, so only a
        # half that ITSELF still fails ends up flagged - the other half
        # still gets a real answer instead of being wasted along with it.
        if len(candidates) > 1:
            print(f"  batch persona/role classification call failed for "
                  f"{len(candidates)} lead(s): {type(e).__name__}: {e} - "
                  f"splitting into two smaller batches and retrying instead "
                  f"of flagging all of them.")
            mid = len(candidates) // 2
            out = {}
            out.update(classify_personas_batch(client, personas, candidates[:mid], icp_text))
            out.update(classify_personas_batch(client, personas, candidates[mid:], icp_text))
            return out
        print(f"  batch persona/role classification call failed for "
              f"{candidates[0]['key']}: {type(e).__name__}: {e} - "
              f"flagging it for review instead.")
        return fallback

    by_key = {}
    for item in items:
        if isinstance(item, dict) and item.get("key") is not None:
            by_key[item["key"]] = item

    out = {}
    for c in candidates:
        item = by_key.get(c["key"])
        if not item:
            out[c["key"]] = fallback[c["key"]]
            continue
        try:
            persona_id = int(item.get("persona_id") or 0)
        except (TypeError, ValueError):
            persona_id = 0
        persona = persona_ids.get(persona_id)  # None covers 0 and any bad/out-of-range id
        role_category = (item.get("role_category") or "").strip().lower()
        if role_category not in ROLE_KEYWORDS:
            role_category = "other"
        try:
            confidence = max(0.0, min(1.0, float(item.get("confidence") or 0.0)))
        except (TypeError, ValueError):
            confidence = 0.0
        out[c["key"]] = {"persona": persona, "role_category": role_category, "confidence": confidence}
    return out


def find_better_contact_at_company(rows, company, exclude_email):
    """Path A of the HR-routing spec: before ever pitching an HR contact
    directly, check whether we already know of a same-company contact
    who's actually a plausible decision-maker (security -> ops/COO -> CEO,
    in that priority order - HR/founder/other/needs_review never count).
    This is deliberately an INTERNAL-ONLY lookaside over rows already
    loaded this run (both tables) - no external people-search API is wired
    up for this (the spec's "enrichment" step), so this is the practical,
    zero-new-dependency MVP of it: if you already have a better contact on
    file for this company, route to them instead of pitching HR; only if
    you genuinely don't, fall back to compose_hr_referral_email() below.
    Returns the best-matching row, or None if nothing usable was found."""
    company_norm = (company or "").strip().lower()
    if not company_norm:
        return None
    exclude_norm = _clean_email(exclude_email)
    candidates = {}
    for r in rows:
        if r.get("_table") != SUPABASE_PERSON_TABLE:
            continue
        if (r.get("company") or "").strip().lower() != company_norm:
            continue
        if _clean_email(r.get("email")) == exclude_norm:
            continue
        if (r.get("status") or "").strip().lower() in ("dead", "rejected"):
            continue
        cat = (r.get("role_category") or "").strip().lower()
        if cat in _HR_ENRICH_PRIORITY and cat not in candidates:
            candidates[cat] = r
    for cat in _HR_ENRICH_PRIORITY:
        if cat in candidates:
            return candidates[cat]
    return None


# --------------------------------------------------------------------------
# compose_hr_referral_email() - FULLY FIXED template, zero free-form AI
# writing. This used to be an AI-composed email (subject + full body, then
# later - after a first bug fix - just the middle "ask" sentence), and
# BOTH versions came back grammatically broken or with the sender's own
# name misspelled ("אני איתי, מצוות 3" came back as "אני מיתי מצוות 3" on
# a real production send). This email's content never actually depends on
# anything specific to the lead (it's a generic "who handles security at
# your company" ask, sent to a general HR/People inbox) - there was never
# a real reason to regenerate its wording from scratch every time, so it
# no longer does. Only {company} and the greeting name are substituted;
# everything else is the literal text below, unchanged from send to send.
# If you want to change the wording, edit _HR_REFERRAL_BODY_TEMPLATE by
# hand and re-read it here - don't turn this back into a model prompt.
# --------------------------------------------------------------------------

_HR_REFERRAL_SUBJECT_TEMPLATE = "שאלה קצרה לגבי {company}"

_HR_REFERRAL_BODY_TEMPLATE = """{greeting}

אני איתי, מצוות 3. אנחנו עוזרים לארגונים עם מצלמות אבטחה להפוך אירועים בזמן אמת להתראות שאפשר לפעול לפיהן, בלי לגעת בתשתית הקיימת.

תוכלו להפנות אותי למי שאחראי אצלכם על נושא האבטחה/המצלמות/ניהול המתקן? אשמח לקבל שם וכתובת דוא"ל כדי שנוכל לדבר על מה שהגיוני עבורכם.

תודה,
איתי"""


def compose_hr_referral_email(client, icp_text, company, email_addr,
                               job_title="", contact_name=""):
    """Path B of the HR-routing spec: used only when
    find_better_contact_at_company() found nobody better on file already.
    A short, standalone ask - never personal_email_templete.txt's persona
    pitch, HR never gets that.

    Fully deterministic (see the module-level comment above) - no AI call,
    no retry, always succeeds. `client`, `icp_text` and `job_title` are
    accepted for backward compatibility with existing call sites but are
    unused; only `company` and `contact_name` (for the greeting) affect the
    output. Kept as a function (not just a dict the caller builds itself)
    so this stays the one place that knows this email's exact wording."""
    greeting = f"היי {contact_name.strip()}," if contact_name and contact_name.strip() else "היי,"
    subject = _HR_REFERRAL_SUBJECT_TEMPLATE.format(company=company or "")
    body = _HR_REFERRAL_BODY_TEMPLATE.format(greeting=greeting)
    return {"subject": subject, "body": body}


FIRST_EMAIL_PERSONA_SYSTEM = """You are filling in specific blanks inside a \
FIRST-touch cold outreach email, as a real salesperson. Stay strictly in \
character, never mention you are an AI, and never invent a fact about the \
recipient/company that isn't given to you below.

You've been given SEVERAL fixed email templates below, each written for a \
different kind of recipient (their job / the industry their company is in). \
Two things to do, in order:

1) PICK exactly one template - whichever genuinely fits this person best, \
based on their job title and whatever LinkedIn/other info is given about \
them below (a bare job title like "COO" or "VP Operations" isn't enough on \
its own to tell industries apart - use that info to decide which \
industry-specific template actually applies). If the job title is blank, \
doesn't clearly fit any template, or you're genuinely unsure, pick \
"{default_label}" instead of forcing a bad match.

2) FILL only the blanks belonging to the template you picked (each blank is \
described by a short instruction next to it). For every blank under every \
OTHER template, return an empty string "" - they will not be used.

LANGUAGE: write every blank in {language} - natural, fluent, native-sounding \
{language} that reads as a seamless continuation of that template's fixed \
text, never a stiff translation.

GRAMMAR CHECK (when writing Hebrew): before answering, re-read every blank \
you wrote and fix any grammar mistake, especially these common ones: \
(1) infinitive verbs always keep their "ל" prefix - never write a bare verb \
like "קחת"/"שמוע"/"ראות" where the correct form is "לקחת"/"לשמוע"/"לראות"; \
(2) when one word governs a list of several items with a prefix like \
"ב-"/"ל-"/"מ-" (e.g. "מתעסקים ב-X, Y ו-Z"), that SAME prefix must repeat on \
every item in the list - never drop it or switch to "ה-"/"ו-" partway \
through; (3) subject-verb number agreement - a plural subject \
("הם"/"הן"/"אתם"/a company referred to in plural) always takes a plural \
verb, e.g. "הם לא מעבירים" never "הם לא מעביר"; (4) grammatical number \
agreement between a numeral and its noun - "מיליוני קילומטרים"/"אלפי \
לקוחות" (plural noun), never "מיליוני קילומטר"/"אלפי לקוח" (singular noun \
after a plural-implying numeral); (5) consistent formality/person across \
ALL blanks in this email - if the email addresses the recipient as \
"אתם"/"לכם" (plural/formal, matching a company), never switch to \
"אתה"/"לך" (singular) in a different blank, and vice versa. Never send a \
sentence with a spelling or grammar error - if you're not sure a sentence \
is fully correct, simplify it rather than risk an error.

STYLE RULES (apply to every blank you fill, in every language): \
(1) never use a dash character ("-" or "־") anywhere in a blank - rephrase \
the sentence instead, e.g. two short sentences instead of one joined by a \
dash; (2) never phrase a blank as a rhetorical question, and never turn it \
into a direct meeting/time request (e.g. "do you have 15 minutes?") - the \
template's own closing line already handles the ask, a blank should never \
try to replace it; (3) never assert a specific fact about the recipient's \
company or infrastructure that isn't actually stated in the research \
below (no camera counts, no "your control room", no specific past \
incident) - if the research doesn't clearly support it, skip that blank \
(return "") rather than guess; (4) never belittle their existing \
equipment; (5) never name a specific Lumana client or award inside a \
blank; (6) never claim a specific past interaction with THIS company \
(e.g. "we already connected to your camera") unless the research below \
actually says so; (7) each template's fixed text already names at most \
ONE concrete product capability - don't add a second one in a blank you \
fill, even if it would otherwise fit.

ONE DELIBERATE EXCEPTION to rules (1) and (2) above: if a blank's own \
instruction tells you to copy, word for word, one of several given fixed \
closing-line options (this is how personal_emails' persona templates now \
ask for their call-to-action), copy your chosen option EXACTLY as written \
in that instruction - including any dash/hyphen character it contains and \
its rhetorical-question phrasing - do not rephrase it, soften it, remove \
the dash from it, or blend two of the options together. That fixed \
closing text is the one place those two style rules don't apply, precisely \
because you were asked to reproduce it verbatim rather than write it \
yourself.

--- PERSONA / ICP (for voice/tone only - each template's own pitch is \
already fixed, don't re-pitch it) ---
{icp}
--- END PERSONA ---

{role_style_note}
--- PERSON ---
Company: {company}
Contact email: {email_addr}
Job title given for this contact: {job_title_note}
Industry on file for their company (if known - use this, together with the \
job title, to help pick the right template; it doesn't replace the company \
research below, which may say more): {industry_note}
Recipient type: {recipient_note}
What's known about them personally (LinkedIn profile and/or other notes on file):
\"\"\"
{research_block}
\"\"\"
What we found about their COMPANY from its own website (use this for context \
about the business, not as something known about them personally - combine \
both when writing, but don't blur the two together):
\"\"\"
{company_research_block}
\"\"\"
--- END PERSON ---

--- TEMPLATES (pick exactly one) ---
{templates_block}
--- END TEMPLATES ---

Respond with ONLY a single JSON object (no markdown fences, no commentary):
  "persona": the exact label you picked, copied character for character \
from one of the "TEMPLATE:" lines above (or "{default_label}")
  ...then one field per blank listed across ALL templates above, named \
exactly as shown (e.g. "p0_slot_1") - "" for every blank that isn't under \
the template you picked.
  "short_body": ONLY if the ROLE-BASED LENGTH GUIDANCE above asked for one \
- a fully independent, free-text ultra-condensed rewrite of the WHOLE email \
(not a template fill - real prose), following every rule above (language, \
style rules, the mandatory fixed closing line copied word for word). \
Omit this key entirely (or leave it "") if no such guidance was given.
"""

# Per-role_category length/tone guidance folded into the system prompt
# above (see compose_person_email()'s role_style_note) - straight from the
# Spikee spec's differentiation table: CEO/founder get an EXTRA ultra-short
# "short_body" rewrite (they're the two categories with no time to read a
# normal-length pitch), everyone else is completely unchanged from before
# this feature existed - same prompt, same template, same length.
ROLE_STYLE_NOTES = {
    "ceo": (
        "ROLE-BASED LENGTH GUIDANCE: this recipient is a CEO/Managing "
        "Director - besides the normal template fill above, ALSO write a "
        '"short_body" field: an independent 3-4 line rewrite of the whole '
        "email (not a blank-fill - free text), containing only: one short "
        "fact/pain sentence, at most one concrete capability (if the "
        "template you picked names one), and the mandatory fixed closing "
        "line copied word for word from one of the three official options "
        "(see the template's own closing blank instruction for the exact "
        "three - copy one exactly, no changes). No elaboration, no long "
        "self-introduction, no repeating the same idea twice - a CEO reads "
        "this in one glance, often past a gatekeeper, so it has to stand "
        "on its own at 3-4 lines."
    ),
    "founder": (
        "ROLE-BASED LENGTH GUIDANCE: this recipient is a Founder/Owner - "
        "besides the normal template fill above, ALSO write a "
        '"short_body" field: an independent short rewrite of the whole '
        "email (not a blank-fill - free text, a little more room than a "
        "CEO's - about 4-5 lines), which MAY include one extra sentence on "
        "how this affects the business as a whole (not just the "
        "site/cameras) since a founder feels both the operational AND the "
        "financial side personally. Still ends on the mandatory fixed "
        "closing line, copied word for word from one of the three official "
        "options in the template's own closing blank instruction. Direct, "
        "personal, action-oriented tone - a founder at a small/mid company "
        "is often the only reader, so there's no gatekeeper to get past, "
        "but there's also no time for a long read."
    ),
    # ops_coo and security are the existing baseline - the Spikee spec says
    # these stay at normal length exactly as the template already is, so
    # they (and "default"/"needs_review"/"hr", which never reaches this
    # function at all) get no note here - system_prompt behaves IDENTICALLY
    # to before this feature existed.
}


def _role_style_block(role_category):
    note = ROLE_STYLE_NOTES.get((role_category or "").strip().lower())
    if not note:
        return ""
    return note


def compose_person_email(client, icp_text, personas, company, email_addr,
                          job_title="", contact_name="", research="",
                          company_research="", recipient_kind="specific_person",
                          role_category="", industry="", forced_persona=None):
    """The ONLY composer personal_emails rows use. Picks whichever persona
    template in personal_email_templete.txt genuinely fits this person, using their
    job_title and linkedin_info (passed in as `research`) PLUS, if a
    company_website scrape is on file, that company's own research summary
    (passed in as `company_research`) - both are handed to the model
    together so the email can draw on the person and their company, and
    fills only that template's blanks - one LLM call. Returns None (never a
    template-based email - there's no fallback template for this table)
    when personal_email_templete.txt is empty or the model can't confidently match a
    persona; the caller is expected to flag that person for your attention
    instead of sending something generic to a named individual.

    `forced_persona`: when given (a label already decided by
    classify_personas_batch() upstream, run once for the whole batch - see
    _run_once()), the model is offered ONLY that one template to fill
    (plus the DEFAULT_PERSONA_LABEL escape hatch, kept as a safety net) -
    it no longer re-decides which persona fits as part of this call. This
    is what lets persona MATCHING happen once, for an entire batch of
    leads, while FILLING the blanks still happens once per lead (it has to
    - the content is personalized) - instead of both happening per-lead,
    inside this same call, the way it used to."""
    if not personas:
        print(f"  no personas defined in {PERSONA_TEMPLATES_FILE.name} - can't write a "
              f"persona email for {contact_name or email_addr}, this run.")
        return None

    personas_to_offer = personas
    if forced_persona:
        if forced_persona not in personas:
            print(f"  forced_persona {forced_persona!r} for {contact_name or email_addr} "
                  f"isn't a known template any more - falling back to a free pick "
                  f"across all templates instead.")
        else:
            personas_to_offer = {forced_persona: personas[forced_persona]}

    rendered = {}  # label -> {"combined": str, "slot_keys": [...]}
    blocks = []
    for i, (label, (subject_t, body_t)) in enumerate(personas_to_offer.items()):
        subj_merged = _merge_simple_fields(subject_t, company, email_addr, contact_name)
        body_merged = _merge_simple_fields(body_t, company, email_addr, contact_name)
        combined = f"{subj_merged}\n\x00\n{body_merged}"
        instructions = [m.group(1).strip() for m in _TEMPLATE_SLOT_RE.finditer(combined)]
        slot_keys = [f"p{i}_slot_{j + 1}" for j in range(len(instructions))]
        rendered[label] = {
            "combined": combined, "slot_keys": slot_keys,
            "instr_by_key": dict(zip(slot_keys, instructions)),
        }

        counter = {"i": 0}

        def to_key(m, keys=slot_keys, counter=counter):
            key = keys[counter["i"]]
            counter["i"] += 1
            return "{{" + key + "}}"

        preview = _TEMPLATE_SLOT_RE.sub(to_key, combined)
        subj_preview, _, body_preview = preview.partition("\n\x00\n")
        # CRITICAL: without this, the model only sees WHERE each blank sits
        # (a bare {{key}} placeholder) with no idea what it's supposed to
        # contain - including, for a fixed-carrier blank, the literal
        # sentence options it must copy word for word (see
        # _VERBATIM_CARRIER_MARKER/find_carrier_violation). Same
        # "Blanks: key: instruction" pattern
        # compose_first_email_from_template() already uses for company_emails.
        slots_block = "\n".join(f"    {k}: {instr}" for k, instr in zip(slot_keys, instructions))
        blocks.append(
            f"TEMPLATE: {label}\n  subject: {subj_preview}\n  body:\n{body_preview}\n"
            f"  Blanks:\n{slots_block}"
        )

    blocks.append(
        f"TEMPLATE: {DEFAULT_PERSONA_LABEL}\n"
        f"  (pick this if the job title is blank, unclear, or doesn't match "
        f"any template above - a general-purpose email is used instead)"
    )

    job_title_note = job_title.strip() if job_title and job_title.strip() else "(not given)"
    industry_note = industry.strip() if industry and industry.strip() else "(not given)"
    research_block = research.strip() if research and research.strip() else (
        "(no research available for this one)"
    )
    company_research_block = company_research.strip() if company_research and company_research.strip() else (
        "(no company website research available for this one)"
    )
    recipient_note = (
        "generic_inbox - this looks like a shared/role inbox (info@, support@, "
        "office@, etc.), not a specific named person."
        if recipient_kind == "generic_inbox" else
        "specific_person - this looks like it belongs to a named individual."
    )
    role_style_note = _role_style_block(role_category)
    short_body_requested = bool(role_style_note)
    if forced_persona and forced_persona in personas_to_offer:
        role_style_note = (
            "NOTE: this lead was already matched to the one template offered "
            "below by an earlier classification pass - you do not need to "
            "judge whether it fits, just fill its blanks well.\n\n"
        ) + role_style_note
    system_prompt = FIRST_EMAIL_PERSONA_SYSTEM.format(
        icp=icp_text, language=EMAIL_LANGUAGE, company=company, email_addr=email_addr,
        job_title_note=job_title_note, industry_note=industry_note, recipient_note=recipient_note,
        research_block=research_block, company_research_block=company_research_block,
        default_label=DEFAULT_PERSONA_LABEL,
        templates_block="\n\n".join(blocks),
        role_style_note=role_style_note,
    )
    result = ask_gemini(client, system_prompt, "Pick a template and fill its blanks now, as the JSON object described above.")

    picked = (result.get("persona") or "").strip()
    if picked not in rendered:
        if picked == DEFAULT_PERSONA_LABEL:
            print(f"  no persona matched {contact_name or email_addr} (job title: "
                  f"{job_title.strip() or '(not given)'}) - personal_emails has no "
                  f"fallback template, flagging for your attention instead.")
        else:
            print(f"  persona pick {picked!r} for {contact_name or email_addr} didn't "
                  f"match a known template - flagging for your attention instead.")
        return None

    info = rendered[picked]

    def _fill_from(one_result):
        counter = {"i": 0}

        def fill_repl(m):
            key = info["slot_keys"][counter["i"]]
            counter["i"] += 1
            text = (one_result.get(key) or "").strip()
            hit = find_banned_phrase(text)
            if hit:
                print(f"  WARNING: a persona template blank contained a banned phrase "
                      f"({hit!r}) - leaving that blank empty instead of sending it.")
                return ""
            carrier_hit = find_carrier_violation(info["instr_by_key"].get(key, ""), text)
            if carrier_hit:
                print(f"  WARNING: a persona template blank {carrier_hit} - leaving "
                      f"that blank empty instead of sending it.")
                return ""
            return text

        filled = _TEMPLATE_SLOT_RE.sub(fill_repl, info["combined"])
        subj, _, bod = filled.partition("\n\x00\n")
        bod = re.sub(r"\n[ \t]*\n[ \t]*\n+", "\n\n", bod).strip()
        return subj, bod

    subject, body = _fill_from(result)
    final_result = result

    hit = find_banned_phrase(subject, body)
    if hit:
        print(f"  WARNING: the assembled persona email still contained a banned "
              f"phrase ({hit!r}) after per-blank checking - not sending it, "
              f"flagging {contact_name or email_addr} for your attention instead.")
        return None

    # Code-level backstop for the "STYLE RULES" section of
    # FIRST_EMAIL_PERSONA_SYSTEM (dashes, rhetorical time-asks, more than
    # one concrete capability - and now also the mandatory fixed closer) -
    # one retry, telling the model exactly what it got wrong, before giving
    # up and flagging this lead for a human instead of sending something
    # that violates the spec.
    violation = find_style_violation(subject, body) or find_closer_violation(body)
    if violation:
        print(f"  persona email for {contact_name or email_addr} violated a style "
              f"rule ({violation}) - asking the model to redo the blanks once.")
        retry_result = ask_gemini(
            client, system_prompt,
            f"Your previous answer picked \"{picked}\" and the assembled email "
            f"violated a STYLE RULE: {violation}. Redo it: pick the SAME template "
            f"({picked!r}) again, but rewrite the blanks so this stops being true. "
            f"Respond with ONLY the JSON object described above.",
        )
        retry_picked = (retry_result.get("persona") or "").strip()
        if retry_picked == picked:
            retry_subject, retry_body = _fill_from(retry_result)
            if not find_banned_phrase(retry_subject, retry_body) and \
               not find_style_violation(retry_subject, retry_body) and \
               not find_closer_violation(retry_body):
                subject, body = retry_subject, retry_body
                final_result = retry_result
                violation = None
            else:
                print(f"  retry still violated a style rule for "
                      f"{contact_name or email_addr} - flagging for your attention "
                      f"instead of sending it.")
        else:
            print(f"  retry picked a different/invalid template ({retry_picked!r}) - "
                  f"flagging {contact_name or email_addr} for your attention instead.")
        if violation:
            return None

    # role_category-based length differentiation (CEO/founder only - see
    # ROLE_STYLE_NOTES/_role_style_block above): use the model's
    # "short_body" rewrite instead of the normal-length body, but ONLY if
    # it actually passes the exact same validation the normal body already
    # had to - one retry on a violation, and if it still can't produce a
    # valid short version, gracefully keep the normal-length body rather
    # than blocking the whole email over a length nicety. Every other
    # role_category (including "" for full backward compatibility) skips
    # this block entirely - subject/body are exactly what they'd have been
    # before this feature existed.
    if short_body_requested:
        def _short_violation(sb):
            if not sb:
                return "short_body was empty/missing"
            return find_banned_phrase(subject, sb) or find_style_violation(subject, sb) \
                or find_closer_violation(sb)

        short_body = (final_result.get("short_body") or "").strip()
        sb_violation = _short_violation(short_body)
        if sb_violation:
            retry_result = ask_gemini(
                client, system_prompt,
                f"Your previous answer's \"short_body\" had a problem: {sb_violation}. "
                f"Redo ONLY that field so this stops being true, keeping everything "
                f"else (including \"persona\" = {picked!r}) the same. Respond with "
                f"ONLY the JSON object described above.",
            )
            short_body = (retry_result.get("short_body") or "").strip()
            sb_violation = _short_violation(short_body)
        if sb_violation:
            print(f"  couldn't get a valid short_body for {contact_name or email_addr} "
                  f"({sb_violation}) - using the normal-length email instead.")
        else:
            body = short_body

    # Proofread pass - but a persona email's closing line is a fixed
    # sentence that MUST survive word for word (find_closer_violation), so
    # never accept a "fix" that rewrote it away, even inadvertently.
    proofed_subject, proofed_body = proofread_email(
        client, subject, body, label=contact_name or email_addr)
    if find_closer_violation(proofed_body):
        print(f"  proofread pass altered the mandatory closing line for "
              f"{contact_name or email_addr} - keeping the pre-proofread draft.")
    else:
        subject, body = proofed_subject, proofed_body

    print(f"  persona match: {picked!r} for {contact_name or email_addr} (job title: {job_title_note})")
    return {"subject": subject, "body": body}


# --------------------------------------------------------------------------
# Batch composition - ONE Gemini call drafts subject+body for SEVERAL
# personal_emails leads at once, instead of one call per lead.
#
# This only works because persona MATCHING already happened earlier, for
# the whole run, in ONE call (classify_personas_batch()) - every lead
# handed to this function already has a forced_persona, so there's nothing
# left to re-decide per lead; the only remaining work (filling in that one
# template's blanks with real, personalized content) is naturally
# batchable, the same way classify_personas_batch() batched the matching
# decision. It's still one lead's content per lead - nobody's email reads
# generically - it's just requested (and returned) for several leads in
# one round trip instead of several separate ones.
#
# Trade-off, stated plainly: compose_person_email() (the single-lead path,
# still used by nothing directly any more but kept for compatibility/
# tests) runs a dedicated style-violation retry per lead, one Gemini call
# at a time. This batched version folds retries for a STYLE violation into
# one shared retry call covering only the leads that failed, not one retry
# call per failure - fewer total calls for that part.
#
# It USED TO also skip the separate proofread_email() pass entirely,
# relying only on the grammar-check instructions baked into the prompt
# below - but that pass turned out to matter: those in-prompt instructions
# only catch mistakes the model recognizes as mistakes while writing, not
# a fluent-LOOKING but nonsensical sentence it produced with a straight
# face (this showed up in real drafts, usually inside the free-text
# "superhuman" personalization line - the one blank with no fixed fallback
# text - when the underlying company_research was thin or messy). So this
# function now DOES run one proofread_email() call per drafted lead too,
# same as the single-lead path (see the loop near the end of this
# function) - it costs one extra call per lead, same as before this batch
# path existed for that specific pass, but it's the only thing that
# actually re-reads the finished email fresh and catches that class of
# error.
# --------------------------------------------------------------------------

def _render_forced_persona(personas, forced_persona, company, email_addr, contact_name, prefix):
    """Same substitution logic as compose_person_email()'s per-persona
    render loop, but for exactly ONE (already-decided) persona, with its
    slot keys namespaced by `prefix` so several people's blanks can share
    one JSON response without colliding (person 0's "slot_1" and person
    1's "slot_1" would otherwise be the same key). Returns None if
    forced_persona isn't a known template any more."""
    if forced_persona not in personas:
        return None
    subject_t, body_t = personas[forced_persona]
    subj_merged = _merge_simple_fields(subject_t, company, email_addr, contact_name)
    body_merged = _merge_simple_fields(body_t, company, email_addr, contact_name)
    combined = f"{subj_merged}\n\x00\n{body_merged}"
    instructions = [m.group(1).strip() for m in _TEMPLATE_SLOT_RE.finditer(combined)]
    slot_keys = [f"{prefix}_slot_{j + 1}" for j in range(len(instructions))]
    counter = {"i": 0}

    def to_key(m, keys=slot_keys, counter=counter):
        key = keys[counter["i"]]
        counter["i"] += 1
        return "{{" + key + "}}"

    preview = _TEMPLATE_SLOT_RE.sub(to_key, combined)
    subj_preview, _, body_preview = preview.partition("\n\x00\n")
    instr_by_key = dict(zip(slot_keys, instructions))
    # CRITICAL: the preview above only shows WHERE each blank sits (as a
    # bare {{key}} placeholder) - it does NOT carry that blank's own
    # instruction text any more, since to_key() replaces it. Without this
    # separate list, the model has no idea what a blank like
    # "{{lead0_slot_3}}" is actually supposed to contain - including, for
    # a fixed-carrier blank, the literal sentence options it's supposed to
    # copy word for word (see _VERBATIM_CARRIER_MARKER/find_carrier_violation)
    # - it can only guess, which is exactly what was happening before this
    # was added: every carrier blank failed validation because the model
    # was never shown what to copy. Same "Blanks: key: instruction" pattern
    # compose_first_email_from_template() already uses for company_emails.
    slots_block = "\n".join(f"    {k}: {instr}" for k, instr in zip(slot_keys, instructions))
    return {
        "combined": combined, "slot_keys": slot_keys,
        "instr_by_key": instr_by_key,
        "preview": f"  subject: {subj_preview}\n  body:\n{body_preview}\n  Blanks:\n{slots_block}",
    }


PERSON_EMAIL_BATCH_SYSTEM = """You are filling in specific blanks inside \
SEVERAL FIRST-touch cold outreach emails at once, one per lead, as a real \
salesperson. Stay strictly in character, never mention you are an AI, and \
never invent a fact about a recipient/company that isn't given for THAT \
lead below - never borrow a fact from one lead's research to use in \
another lead's email.

Each lead below has ALREADY been matched to exactly one email template - \
you are only filling that template's blanks for each lead, not choosing \
between templates.

LANGUAGE: write every blank in {language} - natural, fluent, native-sounding \
{language} that reads as a seamless continuation of that lead's fixed \
template text.

GRAMMAR CHECK (when writing Hebrew): before answering, re-read every blank \
you wrote, for every lead, and fix any grammar mistake, especially: \
(1) infinitive verbs keep their "ל" prefix ("לקחת" not "קחת"); (2) a \
repeated "ב-"/"ל-"/"מ-" prefix across a list of items must repeat on \
every item, not just the first; (3) subject-verb number agreement \
("הם לא מעבירים" not "הם לא מעביר"); (4) numeral-noun number agreement \
("אלפי לקוחות" not "אלפי לקוח"); (5) consistent formality/person within \
ONE lead's email (never mix "אתם"/"אתה" for the same recipient). Never \
send a sentence with a spelling or grammar error - simplify rather than \
risk one.

STYLE RULES (apply to every blank, every lead): \
(1) never use a dash character ("-" or "־") anywhere in a blank; \
(2) never phrase a blank as a rhetorical question or a direct meeting/time \
request - the template's own closing line already handles that; \
(3) never assert a specific fact about a lead's company/infrastructure \
that isn't stated in THAT lead's research below - skip the blank (return \
"") rather than guess; (4) never belittle their existing equipment; \
(5) never name a specific Lumana client or award; (6) never claim a past \
interaction with a company unless that lead's research says so; \
(7) each template's fixed text already names at most ONE concrete product \
capability - don't add a second one.

ONE DELIBERATE EXCEPTION to rules (1) and (2): if a blank's own \
instruction tells you to copy, word for word, one of several given fixed \
closing-line options, copy your chosen option EXACTLY as written - \
including any dash it contains and its rhetorical-question phrasing - do \
not rephrase, soften, or blend two options together.

Some leads' research below is a RAW, unsummarized scrape of their \
company's website (marked "(raw scrape - read and use it yourself, it \
hasn't been summarized)") rather than an already-written summary - for \
those, read it yourself and pull out the same kind of concrete detail a \
summary would have (what they actually do/sell/serve, scale, location) \
before deciding whether a blank that needs it can be filled - if the raw \
text is mostly navigation/boilerplate with nothing concrete, treat it the \
same as "no research available" for that blank.

--- PERSONA / ICP (for voice/tone only - shared across every lead below) ---
{icp}
--- END PERSONA ---

--- LEADS (fill each one's blanks independently) ---
{leads_block}
--- END LEADS ---

Respond with ONLY a single JSON object (no markdown fences, no commentary) \
of this exact shape:
  "results": a JSON array with ONE object per lead above, each with \
"key" (copied character for character from that lead's own "key"), then \
one field per blank listed under that lead's "BLANKS", named exactly as \
shown, plus "short_body" ONLY for a lead whose block says to include one \
(a fully independent, free-text ultra-condensed rewrite of that lead's \
WHOLE email, following every rule above) - omit "short_body" entirely for \
every other lead.
"""


def compose_person_emails_batch(client, icp_text, personas, people):
    """ONE Gemini call that drafts subject+body for SEVERAL personal_emails
    leads at once - the composing counterpart to classify_personas_batch().
    Every lead in `people` must already carry a "forced_persona" decided
    upstream (see classify_personas_batch()/_run_once()) - this never picks
    a persona, only fills the one it's given.

    `people`: a list of dicts, each with "key" (unique - the row's email is
    a good choice), "company", "email_addr", "job_title", "contact_name",
    "research" (linkedin_info/personal notes), "company_research" (an
    already-summarized research_company() result), "raw_scrape" (an
    UNSUMMARIZED website scrape - pass whichever of these two you have, not
    both; if both are blank that's fine too), "recipient_kind",
    "role_category", "industry", "forced_persona".

    Returns {key: {"subject": str, "body": str}} - only for leads that
    ended up with a valid draft. A lead missing from the result (dropped by
    the model, or its draft failed style validation even after the shared
    retry) simply isn't in the returned dict - the caller flags it for
    human attention, same as a None from compose_person_email().

    Raises if the underlying call to the model itself never produced a
    usable response at all - a network error (timeout, connection dropped
    mid-request, etc.) or a malformed/unparseable response shape. This is
    deliberate: that class of failure has nothing to do with any lead's
    content, so it should NOT be reported (or treated) the same way as a
    real per-lead validation failure - the caller (_run_once()) already
    catches this around its own call to this function and leaves every
    lead in the chunk as "new" for an automatic retry next run, instead of
    permanently flagging them needs_attention with a misleading "failed
    style validation" note for what was really just a dropped connection."""
    if not people:
        return {}
    if not personas:
        print(f"  no personas defined in {PERSONA_TEMPLATES_FILE.name} - can't batch-compose "
              f"{len(people)} lead(s) this run.")
        return {}

    rendered_by_key = {}
    leads_blocks = []
    short_body_wanted = {}
    for i, p in enumerate(people):
        prefix = f"lead{i}"
        info = _render_forced_persona(
            personas, p["forced_persona"], p.get("company") or "",
            p["email_addr"], p.get("contact_name") or "", prefix,
        )
        if info is None:
            print(f"  forced_persona {p['forced_persona']!r} for {p['key']} isn't a "
                  f"known template any more - skipping it from this batch compose call.")
            continue
        rendered_by_key[p["key"]] = info

        job_title_note = (p.get("job_title") or "").strip() or "(not given)"
        industry_note = (p.get("industry") or "").strip() or "(not given)"
        research_block = (p.get("research") or "").strip() or "(no research available for this one)"
        raw_scrape = (p.get("raw_scrape") or "").strip()
        company_research = (p.get("company_research") or "").strip()
        if raw_scrape and not company_research:
            company_research_block = (
                "(raw scrape - read and use it yourself, it hasn't been summarized)\n" + raw_scrape
            )
        elif company_research:
            company_research_block = company_research
        else:
            company_research_block = "(no company website research available for this one)"
        recipient_note = (
            "generic_inbox - shared/role inbox, not a specific named person."
            if p.get("recipient_kind") == "generic_inbox" else
            "specific_person - belongs to a named individual."
        )
        role_style_note = _role_style_block(p.get("role_category"))
        if role_style_note:
            short_body_wanted[p["key"]] = True

        leads_blocks.append(
            f'LEAD key="{p["key"]}"\n'
            f"  Company: {p.get('company') or ''}\n"
            f"  Contact email: {p['email_addr']}\n"
            f"  Job title: {job_title_note}\n"
            f"  Industry on file: {industry_note}\n"
            f"  Recipient type: {recipient_note}\n"
            f"  {role_style_note or ''}"
            f'  What\'s known about them personally:\n  """\n  {research_block}\n  """\n'
            f'  Company research:\n  """\n  {company_research_block}\n  """\n'
            f"  TEMPLATE (already matched - just fill it):\n{info['preview']}\n"
        )

    if not rendered_by_key:
        return {}

    system_prompt = PERSON_EMAIL_BATCH_SYSTEM.format(
        icp=icp_text, language=EMAIL_LANGUAGE, leads_block="\n\n".join(leads_blocks),
    )
    # NOTE: unlike a per-lead validation failure below (banned phrase, style
    # violation, closer violation - which legitimately means "the model
    # tried and the result wasn't good enough, a human should look"), a
    # failure HERE means the call to the model itself never produced
    # anything usable at all - a network hiccup (e.g. the ConnectionError/
    # RemoteDisconnected you can get from any HTTP call, nothing to do with
    # the content), a timeout, or a malformed response shape. That's a
    # completely different situation and used to be handled the same way
    # (silently return {} and let every lead in the chunk look, to the
    # caller, exactly like "the assembled email failed style validation" -
    # a misleading needs_attention note that also permanently pulled a
    # perfectly fine lead out of the retry queue over what was really just
    # bad luck on one network call). So these two failure modes now RAISE
    # instead of swallowing the error - _run_once()'s own try/except around
    # this whole function call already exists specifically to catch this:
    # it leaves every lead in the chunk as "new" and logs the real error,
    # so a transient failure here gets a clean automatic retry on the next
    # run instead of a wrong, permanent "needs a human look".
    result = ask_gemini(
        client, system_prompt,
        "Fill every lead's blanks now, as the JSON object described above.",
    )

    items = result.get("results") if isinstance(result, dict) else None
    if not isinstance(items, list):
        raise ValueError(
            f"batch compose call for {len(rendered_by_key)} lead(s) returned something "
            f"that wasn't the expected {{\"results\": [...]}} JSON shape"
        )
    by_key = {item["key"]: item for item in items if isinstance(item, dict) and item.get("key") is not None}

    def _fill_one(key, one_result):
        info = rendered_by_key[key]
        counter = {"i": 0}

        def fill_repl(m):
            slot_key = info["slot_keys"][counter["i"]]
            counter["i"] += 1
            text = (one_result.get(slot_key) or "").strip()
            hit = find_banned_phrase(text)
            if hit:
                print(f"  WARNING: a persona template blank for {key} contained a banned "
                      f"phrase ({hit!r}) - leaving that blank empty instead of sending it.")
                return ""
            carrier_hit = find_carrier_violation(info["instr_by_key"].get(slot_key, ""), text)
            if carrier_hit:
                print(f"  WARNING: a persona template blank for {key} {carrier_hit} - "
                      f"leaving that blank empty instead of sending it.")
                return ""
            return text

        filled = _TEMPLATE_SLOT_RE.sub(fill_repl, info["combined"])
        subj, _, bod = filled.partition("\n\x00\n")
        bod = re.sub(r"\n[ \t]*\n[ \t]*\n+", "\n\n", bod).strip()
        return subj, bod

    drafts = {}
    violators = []  # keys that need one shared retry
    for key in rendered_by_key:
        one_result = by_key.get(key)
        if not one_result:
            print(f"  batch compose call didn't return anything for {key} - "
                  f"flagging for your attention instead.")
            continue
        subject, body = _fill_one(key, one_result)
        if find_banned_phrase(subject, body):
            print(f"  the assembled persona email for {key} still contained a banned "
                  f"phrase after per-blank checking - flagging for your attention instead.")
            continue
        violation = find_style_violation(subject, body) or find_closer_violation(body)
        if violation:
            violators.append((key, violation))
            continue
        drafts[key] = {"subject": subject, "body": body, "_result": one_result}

    if violators:
        retry_leads = "\n".join(
            f'LEAD key="{key}": your previous answer violated a STYLE RULE: {violation}. '
            f"Redo ONLY this lead's blanks so this stops being true, keeping everything else "
            f"the same."
            for key, violation in violators
        )
        print(f"  {len(violators)} persona email(s) in this batch violated a style rule - "
              f"asking the model to redo just those, once.")
        try:
            retry_result = ask_gemini(
                client, system_prompt,
                f"Some leads' answers violated a style rule. Redo ONLY these leads' blanks "
                f"(keep every other lead's answer out of your response entirely):\n{retry_leads}\n"
                f"Respond with ONLY the JSON object described above, containing just the "
                f"leads listed here.",
            )
            retry_items = retry_result.get("results") if isinstance(retry_result, dict) else None
            retry_by_key = {
                item["key"]: item for item in (retry_items or [])
                if isinstance(item, dict) and item.get("key") is not None
            } if isinstance(retry_items, list) else {}
        except Exception as e:
            print(f"  batch retry call failed: {type(e).__name__}: {e} - flagging the "
                  f"{len(violators)} violator(s) for your attention instead.")
            retry_by_key = {}
        for key, _ in violators:
            retry_one = retry_by_key.get(key)
            if not retry_one:
                print(f"  retry didn't return anything for {key} - flagging for your "
                      f"attention instead of sending it.")
                continue
            subject, body = _fill_one(key, retry_one)
            if find_banned_phrase(subject, body) or find_style_violation(subject, body) \
                    or find_closer_violation(body):
                print(f"  retry still violated a style rule for {key} - flagging for "
                      f"your attention instead of sending it.")
                continue
            drafts[key] = {"subject": subject, "body": body, "_result": retry_one}

    # short_body, for whichever leads asked for one (CEO/founder) - best
    # effort, no extra retry call: an invalid/missing short_body just keeps
    # the normal-length body, same graceful fallback as the single-lead path.
    for key, draft in drafts.items():
        if key not in short_body_wanted:
            continue
        one_result = draft.pop("_result", {})
        short_body = (one_result.get("short_body") or "").strip()
        if not short_body:
            continue
        if find_banned_phrase(draft["subject"], short_body) or \
           find_style_violation(draft["subject"], short_body) or \
           find_closer_violation(short_body):
            print(f"  couldn't get a valid short_body for {key} - using the normal-length "
                  f"email instead.")
            continue
        draft["body"] = short_body
    for draft in drafts.values():
        draft.pop("_result", None)

    # Proofread pass - one call per draft, same backstop compose_person_email()
    # (the single-lead path) has always run. This batched composer used to
    # skip it entirely to save calls (see the block comment above this
    # function), relying only on the GRAMMAR CHECK instructions baked into
    # PERSON_EMAIL_BATCH_SYSTEM - but those instructions only catch mistakes
    # the model recognizes as mistakes; they didn't catch it producing a
    # fluent-LOOKING but nonsensical sentence (usually inside the free-text
    # "superhuman" personalization line, the one blank with no fixed answer
    # to fall back on) when the underlying company_research was thin or
    # messy. A dedicated proofread call, reading the ALREADY-ASSEMBLED email
    # fresh with no other context, catches that class of error the way a
    # human proofreader would. Same guard as the single-lead path: a
    # persona email's closing line is a fixed sentence that MUST survive
    # word for word, so a "fix" that rewrote it away (even inadvertently)
    # is rejected and the pre-proofread draft is kept instead.
    for key, draft in drafts.items():
        proofed_subject, proofed_body = proofread_email(
            client, draft["subject"], draft["body"], label=key)
        if find_closer_violation(proofed_body):
            print(f"  proofread pass altered the mandatory closing line for {key} - "
                  f"keeping the pre-proofread draft.")
            continue
        draft["subject"], draft["body"] = proofed_subject, proofed_body

    return drafts


def compose_reply(client, icp_text, company, email_addr, their_message, offered_slots,
                   sent_at=None):
    slots_block = "none offered yet"
    if offered_slots:
        slots_block = "\n".join(
            f"  [{i}] {_fmt_local(s['start'])}" for i, s in enumerate(offered_slots)
        )
    tz_name = os.getenv("TIMEZONE", "Asia/Jerusalem")
    now_local = datetime.now(_local_tz())
    # Resolve "tomorrow"/"Thursday"/etc. against when THEY actually sent the
    # message, not against whenever the bot happens to get around to
    # processing it - a reply that sits for a day or two before being
    # scanned would otherwise have "tomorrow" silently shift to the wrong
    # date (and can land on a non-work day that was never what they meant).
    anchor = sent_at.astimezone(_local_tz()) if sent_at else now_local
    anchor_note = (
        "" if anchor == now_local else
        f" (it is now {now_local.strftime('%A, %Y-%m-%d %H:%M')} local, but "
        f"resolve their relative dates against WHEN THEY WROTE IT, not now - "
        f"we're just processing it a bit late)"
    )
    user_prompt = (
        f"Company: {company}\nEmail address: {email_addr}\n\n"
        f"They sent this message at {anchor.strftime('%A, %Y-%m-%d %H:%M')} local "
        f"time ({tz_name}){anchor_note} - resolve any relative day/time they "
        f"mention (\"tomorrow\", \"Thursday\", \"in an hour\") against that "
        f"send time.\n\n"
        f"Their latest message:\n\"\"\"\n{their_message}\n\"\"\"\n\n"
        f"OFFERED SLOTS (only relevant if we already sent these):\n{slots_block}\n\n"
        f"Write the reply. If slots WERE already offered and their message picks "
        f"one, MATCH IT TO ONE OF THE OFFERED SLOTS ABOVE by index and set "
        f"picked_slot_index accordingly, confirming it in the body - this applies "
        f"NO MATTER HOW they phrase it: a bare time with no day mentioned (\"10:00 "
        f"works\", \"the first one\"), a day only (\"Thursday works\" when only one "
        f"offered slot is on a Thursday), OR a full day+time that matches one of "
        f"the offered slots exactly or nearly exactly (e.g. they write \"18.9 at "
        f"10:00\" and slot [0] IS 18.9 at 10:00 - that is a MATCH, not a new "
        f"proposal, even though they mentioned the day explicitly). Matching by "
        f"index off the OFFERED SLOTS list above is always more reliable than "
        f"recomputing a date yourself, so whenever what they wrote corresponds to "
        f"one of the listed slots - by day, by time, or both - prefer "
        f"picked_slot_index over proposed_datetime, even if they restated the "
        f"date instead of just saying a bare time. Do NOT invent your own date "
        f"for something that already matches an offered slot. Only use "
        f"proposed_datetime when they propose a genuinely NEW specific day/time "
        f"that does NOT correspond to any offered slot (whether or not slots were "
        f"offered) - e.g. \"does tomorrow at 4pm work?\" when 4pm tomorrow wasn't "
        f"one of the options - and DON'T confirm it in the body yet, since the "
        f"script still needs to check the real calendar; just write something like "
        f"\"let me check and get back to you\" (still in persona). Otherwise, if "
        f"they want a meeting but haven't proposed or picked a time, don't mention "
        f"specific times yourself - just say you'll send a couple of options, and "
        f"set wants_meeting=true (the script will fetch real slots and you'll be "
        f"asked again to phrase the offer)."
    )
    return ask_gemini(client, COMPOSER_SYSTEM.format(icp=icp_text, language=EMAIL_LANGUAGE), user_prompt)


def compose_slot_offer(client, icp_text, company, email_addr, their_message, slots):
    slots_block = "\n".join(f"  [{i}] {_fmt_local(s['start'])}" for i, s in enumerate(slots))
    user_prompt = (
        f"Company: {company}\nEmail address: {email_addr}\n\n"
        f"Their message:\n\"\"\"\n{their_message}\n\"\"\"\n\n"
        f"They want to meet. Here are real open slots from the calendar "
        f"(local time, {os.getenv('TIMEZONE', 'Asia/Jerusalem')}):\n{slots_block}\n\n"
        f"Write a short reply offering these as options and asking them to pick "
        f"one. wants_meeting=true, picked_slot_index=null, proposed_datetime=null, "
        f"not_interested=false, redirect_email=null, ask_for_referral=false, "
        f"alert_needed=false, alert_reason=null."
    )
    return ask_gemini(client, COMPOSER_SYSTEM.format(icp=icp_text, language=EMAIL_LANGUAGE), user_prompt)


def compose_calendly_offer(client, icp_text, company, email_addr, their_message, calendly_link):
    """The "no zigzag" reply: instead of proposing/confirming times
    ourselves, send one message with the Calendly link and let them pick a
    real slot directly - no back-and-forth, no calendar check on our end."""
    user_prompt = (
        f"Company: {company}\nEmail address: {email_addr}\n\n"
        f"Their message:\n\"\"\"\n{their_message}\n\"\"\"\n\n"
        f"They want to meet (or proposed a time of their own). Instead of "
        f"proposing or confirming any specific day/time yourself, write a "
        f"short reply that hands them this scheduling link so THEY pick "
        f"whatever slot works for them: {calendly_link}\n"
        f"Weave the link in naturally and briefly (e.g. \"here's my calendar - "
        f"grab whatever time works best for you: {calendly_link}\"). Do not "
        f"list times, do not ask them to reply with a day/time, and do not "
        f"say you'll follow up with options - the link is the whole ask. "
        f"wants_meeting=true, picked_slot_index=null, proposed_datetime=null, "
        f"not_interested=false, redirect_email=null, ask_for_referral=false, "
        f"alert_needed=false, alert_reason=null."
    )
    return ask_gemini(client, COMPOSER_SYSTEM.format(icp=icp_text, language=EMAIL_LANGUAGE), user_prompt)


def compose_time_confirm(client, icp_text, company, email_addr, their_message, start_dt):
    tz_name = os.getenv("TIMEZONE", "Asia/Jerusalem")
    user_prompt = (
        f"Company: {company}\nEmail address: {email_addr}\n\n"
        f"Their message:\n\"\"\"\n{their_message}\n\"\"\"\n\n"
        f"They proposed {start_dt.strftime('%A, %Y-%m-%d %H:%M')} ({tz_name}) for "
        f"a call, and that time is open on the calendar and has been booked. Write "
        f"a short reply confirming that time works and that it's on the calendar. "
        f"wants_meeting=true, picked_slot_index=null, proposed_datetime=null, "
        f"not_interested=false, redirect_email=null, ask_for_referral=false, "
        f"alert_needed=false, alert_reason=null."
    )
    return ask_gemini(client, COMPOSER_SYSTEM.format(icp=icp_text, language=EMAIL_LANGUAGE), user_prompt)


def compose_time_unavailable(client, icp_text, company, email_addr, their_message,
                              start_dt, alt_slots):
    tz_name = os.getenv("TIMEZONE", "Asia/Jerusalem")
    alts_block = "\n".join(f"  [{i}] {_fmt_local(s['start'])}" for i, s in enumerate(alt_slots))
    user_prompt = (
        f"Company: {company}\nEmail address: {email_addr}\n\n"
        f"Their message:\n\"\"\"\n{their_message}\n\"\"\"\n\n"
        f"They proposed {start_dt.strftime('%A, %Y-%m-%d %H:%M')} ({tz_name}) for "
        f"a call, but that time is NOT actually open on the calendar. Write a "
        f"short, friendly reply saying that specific time doesn't work and "
        f"offering these real open alternatives instead (local time, {tz_name}):\n"
        f"{alts_block}\n\n"
        f"wants_meeting=true, picked_slot_index=null, proposed_datetime=null, "
        f"not_interested=false, redirect_email=null, ask_for_referral=false, "
        f"alert_needed=false, alert_reason=null."
    )
    return ask_gemini(client, COMPOSER_SYSTEM.format(icp=icp_text, language=EMAIL_LANGUAGE), user_prompt)


def _send_calendar_unavailable_fallback(ws, header, rep, draft, when_str, error):
    """Calendar auth is missing/broken. Still gets SOMETHING back to the
    lead - the soft "let me check and get back to you" reply the composer
    already drafted - instead of leaving them hanging with silence, and
    flags the row clearly so a human notices it needs a manual booking."""
    print(f"  calendar isn't available for {rep['email']}'s reply: {error}")
    note = "calendar isn't connected (run: python authorize_calendar.py step1) - book manually"
    if when_str:
        note = f"they proposed {when_str} but {note}"
    msg_id = send_one(rep["email"], draft["subject"], draft["body"], in_reply_to=rep["message_id"])
    update_lead(ws, header, rep["row"], status="awaiting_slot", message_id=msg_id, notes=note)
    log_activity("SENT reply (calendar unavailable - book manually)", rep["company"], rep["email"],
                 draft["subject"], draft["body"])
    print(f"  sent a holding reply to {rep['email']} - {note}")


def _handle_reply(client, icp_text, ws, header, rep, lead_state, offered,
                   progress=None, idx=0, total=0, label=""):
    """Everything for ONE lead's reply: booking a picked/proposed slot,
    offering real slots, or a plain back-and-forth reply. Calendar auth
    failures degrade to a fallback reply rather than raising - the caller
    still wraps this for anything else that could go wrong (a bad Gemini
    response, a network hiccup), so one lead's problem never takes the
    whole run down with it.

    progress/idx/total/label (all optional) let this report finer-grained
    sub-steps (checking the calendar, sending) back through the same
    "replies" stage the caller is already tracking, so the dashboard's
    progress bar can show what's actually happening instead of sitting
    still for the whole lead."""
    if progress is None:
        progress = lambda *a, **k: None

    draft = compose_reply(
        client, icp_text, rep["company"], rep["email"], rep["message"], offered,
        sent_at=rep.get("sent_at"),
    )

    # (alert) something here needs YOUR judgment - stop everything else for
    # this lead. Nothing is sent to them (per your call: stay silent rather
    # than risk an automated reply making a sensitive situation worse), and
    # NONE of the branches below run this pass - not booking, not the
    # redirect handoff, nothing - so a message that trips this can't also
    # trigger some other automatic action in the same breath. You get a
    # real email about it immediately; the lead just doesn't hear back
    # until you've looked at it and (if you want the bot back on this
    # thread) moved their status off "needs_attention" yourself.
    if draft.get("alert_needed"):
        reason = (draft.get("alert_reason") or "").strip() or "not sure why - check the reply yourself"
        update_lead(ws, header, rep["row"], status="needs_attention",
                     notes=f"flagged for your attention: {reason}")
        log_activity("ALERT (needs your attention)", rep["company"], rep["email"],
                     subject=reason, body=rep["message"])
        send_alert_email(rep["company"], rep["email"], reason, rep["message"])
        print(f"  ALERT: {rep['email']} flagged for your attention - {reason} "
              f"(no reply sent, emailed you about it)")
        return

    # (redirect) they handed us off to someone else's email - independent of
    # whatever else this reply does (booking a slot, declining, etc.), so
    # this runs unconditionally and doesn't `return`. Adds the new address
    # as its own lead and sends it a warm handoff pitch, mentioning it came
    # via this contact.
    redirect_raw = draft.get("redirect_email")
    if redirect_raw:
        redirect_email = _clean_email(redirect_raw)
        if not EMAIL_RE.match(redirect_email):
            print(f"  ignoring redirect_email {redirect_raw!r} for {rep['email']} - "
                  f"doesn't look like a real address")
        elif redirect_email in (_clean_email(rep["email"]), _clean_email(GMAIL_USER)):
            print(f"  ignoring redirect_email {redirect_raw!r} for {rep['email']} - "
                  f"same as the sender or our own address")
        elif _find_lead_row_by_email(ws, header, redirect_email) is not None:
            print(f"  {redirect_email} was already a lead - skipping duplicate "
                  f"referral from {rep['email']}")
        else:
            _report(progress, "replies", idx, total, label, "referral")
            ref_draft = compose_referral_email(
                client, icp_text, rep["company"], rep["email"], redirect_email
            )
            # The new row lands in whichever table the original reply came
            # from - a company_emails handoff creates another company_emails
            # row (template pitch), a personal_emails handoff creates
            # another personal_emails row (persona pitch). This keeps every
            # row's table telling you which composer wrote its email,
            # exactly like every other row.
            referral_table = rep.get("_table") or SUPABASE_COMPANY_TABLE
            if REVIEW_MODE:
                new_row = add_lead_row(
                    ws, header, referral_table, company=rep["company"], email=redirect_email,
                    status="draft_pending", draft_subject=ref_draft["subject"],
                    draft_body=ref_draft["body"],
                    notes=f"referred by {rep['email']} ({rep['company']})",
                )
                log_activity("DRAFT referral (awaiting your approval)", rep["company"],
                             redirect_email, ref_draft["subject"], ref_draft["body"])
                print(f"DRAFTED referral (not sent, awaiting approval) -> {redirect_email} "
                      f"(via {rep['email']})")
            else:
                msg_id = send_one(redirect_email, ref_draft["subject"], ref_draft["body"])
                new_row = add_lead_row(
                    ws, header, referral_table, company=rep["company"], email=redirect_email,
                    status="contacted", message_id=msg_id,
                    notes=f"referred by {rep['email']} ({rep['company']})",
                )
                log_activity("SENT referral email", rep["company"], redirect_email,
                             ref_draft["subject"], ref_draft["body"])
                print(f"SENT referral email -> {redirect_email} (via {rep['email']})")

    wants_to_meet = bool(draft.get("wants_meeting")) or bool(draft.get("proposed_datetime"))

    # (a) Calendly configured: no zigzag. One reply with the booking link,
    # whether they said "let's meet" or proposed their own time - either
    # way we don't check/offer calendar slots ourselves anymore. Calendly's
    # own page handles picking a time; poll_calendly_bookings() (called
    # once per scan, see _run_once) is what notices they actually booked
    # and marks this lead "booked".
    if wants_to_meet and CALENDLY_LINK:
        offer = compose_calendly_offer(
            client, icp_text, rep["company"], rep["email"], rep["message"], CALENDLY_LINK
        )
        _report(progress, "replies", idx, total, label, "sending")
        msg_id = send_one(rep["email"], offer["subject"], offer["body"],
                           in_reply_to=rep["message_id"])
        update_lead(ws, header, rep["row"], status="awaiting_booking", message_id=msg_id,
                    notes="sent Calendly link - waiting for them to book")
        log_activity("SENT Calendly link", rep["company"], rep["email"],
                     offer["subject"], offer["body"])
        print(f"SENT Calendly link to {rep['email']}")
        return

    # --- everything below only runs when CALENDLY_LINK isn't set in .env -
    # the old Google-Calendar-driven propose/confirm flow, kept as a
    # fallback so the bot still works before Calendly is configured. ---

    # (a-old) they confirmed one of the slots we already offered them
    if not CALENDLY_LINK and draft.get("picked_slot_index") is not None and offered:
        picked_idx = draft["picked_slot_index"]
        if 0 <= picked_idx < len(offered):
            slot = offered[picked_idx]
            _report(progress, "replies", idx, total, label, "calendar")
            try:
                created = book_slot(rep["email"], rep["company"], slot["start"], slot["end"])
            except CalendarNotConfigured as e:
                _send_calendar_unavailable_fallback(ws, header, rep, draft, slot["start"], e)
                return
            _report(progress, "replies", idx, total, label, "sending")
            msg_id = send_one(rep["email"], draft["subject"], draft["body"],
                               in_reply_to=rep["message_id"])
            update_lead(ws, header, rep["row"], status="booked", message_id=msg_id,
                        notes=f"booked {slot['start']} (event {created.get('id')})")
            log_activity("MEETING BOOKED", rep["company"], rep["email"],
                         draft["subject"], f"{draft['body']}\n\n(slot: {slot['start']})")
            print(f"BOOKED meeting with {rep['email']} at {slot['start']}")
            return

    # (b-old) they proposed a specific time of their own (e.g. "free
    # tomorrow at 4?") instead of picking from a list we sent - check the
    # real calendar for that exact time rather than making them choose from
    # generic options
    raw_proposed = draft.get("proposed_datetime")
    proposed_dt = _parse_local_datetime(raw_proposed) if not CALENDLY_LINK else None
    if raw_proposed and not proposed_dt and not CALENDLY_LINK:
        print(f"  Model proposed a time ({raw_proposed!r}) for {rep['email']} that "
              f"couldn't be parsed or was in the past - falling back to offering real slots.")
    if proposed_dt:
        start_dt = proposed_dt
        end_dt = start_dt + timedelta(minutes=30)
        _report(progress, "replies", idx, total, label, "calendar")
        try:
            service = get_calendar_service()
            free = _slot_is_free(service, start_dt, end_dt)
        except CalendarNotConfigured as e:
            _send_calendar_unavailable_fallback(ws, header, rep, draft, start_dt.isoformat(), e)
            return
        if free:
            confirm = compose_time_confirm(
                client, icp_text, rep["company"], rep["email"], rep["message"], start_dt
            )
            created = book_slot(rep["email"], rep["company"],
                                 start_dt.isoformat(), end_dt.isoformat())
            _report(progress, "replies", idx, total, label, "sending")
            msg_id = send_one(rep["email"], confirm["subject"], confirm["body"],
                               in_reply_to=rep["message_id"])
            update_lead(ws, header, rep["row"], status="booked", message_id=msg_id,
                        notes=f"booked {start_dt.isoformat()} (event {created.get('id')}, "
                              f"lead proposed the time themselves)")
            log_activity("MEETING BOOKED (lead's proposed time)", rep["company"],
                         rep["email"], confirm["subject"],
                         f"{confirm['body']}\n\n(slot: {start_dt.isoformat()})")
            print(f"BOOKED meeting with {rep['email']} at {start_dt.isoformat()} "
                  f"(the time they proposed)")
        else:
            alt_slots = _find_open_slots(service, 3)
            unavail = compose_time_unavailable(
                client, icp_text, rep["company"], rep["email"], rep["message"],
                start_dt, alt_slots
            )
            _report(progress, "replies", idx, total, label, "sending")
            msg_id = send_one(rep["email"], unavail["subject"], unavail["body"],
                               in_reply_to=rep["message_id"])
            lead_state["offered_slots"] = alt_slots
            update_lead(ws, header, rep["row"], status="awaiting_slot", message_id=msg_id,
                        notes=f"their proposed time ({start_dt.isoformat()}) wasn't free; "
                              f"offered alternatives")
            log_activity("SENT alternatives (their proposed time was unavailable)",
                         rep["company"], rep["email"], unavail["subject"], unavail["body"])
            print(f"{rep['email']}'s proposed time was busy - offered alternatives")
        return

    # (c-old) they want a meeting but haven't proposed or picked a specific
    # time - fall back to sending 3 real open slots to choose from
    if not CALENDLY_LINK and draft.get("wants_meeting") and not offered:
        _report(progress, "replies", idx, total, label, "calendar")
        try:
            slots = get_open_slots(count=3)
        except CalendarNotConfigured as e:
            _send_calendar_unavailable_fallback(ws, header, rep, draft, None, e)
            return
        slot_draft = compose_slot_offer(
            client, icp_text, rep["company"], rep["email"], rep["message"], slots
        )
        _report(progress, "replies", idx, total, label, "sending")
        msg_id = send_one(rep["email"], slot_draft["subject"], slot_draft["body"],
                           in_reply_to=rep["message_id"])
        lead_state["offered_slots"] = slots
        update_lead(ws, header, rep["row"], status="awaiting_slot", message_id=msg_id)
        log_activity("SENT slot offer", rep["company"], rep["email"],
                     slot_draft["subject"], slot_draft["body"])
        print(f"OFFERED slots to {rep['email']}")
        return

    # (d) plain back-and-forth reply. Code-level backstop, on top of the
    # prompt instructions above: this is exactly the kind of reply that
    # produced the "removed you from the distribution list" incident once
    # already, so it gets a banned-phrase check no matter how well-prompted
    # the model is supposed to be by now.
    subject, body = draft["subject"], draft["body"]
    hit = find_banned_phrase(subject, body)
    if hit:
        print(f"  WARNING: a reply to {rep['email']} contained a banned phrase "
              f"({hit!r}) despite the prompt instruction - sending a safe fallback "
              f"instead. Worth checking if this keeps happening.")
        subject = draft["subject"] if not find_banned_phrase(draft["subject"]) else "Re: your message"
        body = "No worries, thanks for letting me know." if draft.get("not_interested") \
            else "Thanks for the reply - let me look into that."
    _report(progress, "replies", idx, total, label, "sending")
    msg_id = send_one(rep["email"], subject, body, in_reply_to=rep["message_id"])
    if draft.get("not_interested"):
        # Clear decline/unsubscribe - close this out for good, UNLESS the
        # closing message itself just asked them for a referral (see
        # ask_for_referral in COMPOSER_SYSTEM): in that case we're still
        # waiting on a possible reply with an email address, so this stays
        # in STATUSES_CONTACTABLE ("awaiting_referral") instead of "dead" -
        # marking it dead here would mean their next message (hopefully
        # containing exactly the address we asked for) gets silently
        # ignored by check_inbox_for_replies. redirect_raw is truthy only
        # when they ALREADY handed over an address in this same message
        # (handled above, unconditionally) - in that case the handoff is
        # already done, so this closes out as dead like normal.
        if draft.get("ask_for_referral") and not redirect_raw:
            status = "awaiting_referral"
            note = f"declined for themselves, asked if they could point us to someone: {rep['message'][:200]!r}"
            kind = "SENT closing reply (asked for a referral)"
        else:
            # "dead" is NOT in STATUSES_CONTACTABLE, so any future email
            # from this address (even a resend of the same "not interested"
            # message) is no longer picked up as a reply worth processing -
            # this is what actually stops the endless reply loop.
            status = "dead"
            note = f"marked not interested - final reply: {rep['message'][:200]!r}"
            kind = "SENT closing reply (not interested - marked dead)"
        update_lead(ws, header, rep["row"], status=status, message_id=msg_id, notes=note)
        log_activity(kind, rep["company"], rep["email"], subject, body)
        print(f"NOT INTERESTED - {rep['email']} -> status={status}")
    else:
        update_lead(ws, header, rep["row"], status="replied", message_id=msg_id)
        log_activity("SENT reply", rep["company"], rep["email"], subject, body)
        print(f"REPLIED to {rep['email']}")


def cmd_run(args):
    with LEADS_LOCK:
        _run_once()
    # Deliberately AFTER the block above - _run_once() has already printed
    # "Done. N sent, ..." by the time it returns, so this always runs
    # after that notice, never before or in the middle of it.
    run_prefetch_after_run()


def _run_once(progress=None):
    """progress, if given, is called as progress(stage, current, total, label,
    step="") at each meaningful point - stage is one of "leads",
    "inbox_check", "replies", "done". step is an optional finer-grained
    sub-step within the current lead/reply (e.g. "liveness", "website",
    "compose", "calendar", "sending") so a dashboard can show what's
    actually happening right now, not just which lead. Purely
    optional/cosmetic (the dashboard uses it to drive a progress bar); the
    CLI ("python outreach_bot.py run") doesn't pass one, so it's a no-op
    there and behavior is unchanged."""
    if progress is None:
        progress = lambda *a, **k: None

    if not ICP_FILE.exists():
        sys.exit(f"{ICP_FILE} not found - fill in the persona file first.")
    icp_text = build_persona_text(ICP_FILE.read_text(encoding="utf-8"))
    if "Company name:" in icp_text and len(icp_text) < 1200:
        print("Warning: icp.txt still looks like the blank template - "
              "the emails will be generic until you fill it in.")

    if not EMAIL_TEMPLATE_FILE.exists():
        EMAIL_TEMPLATE_FILE.write_text(DEFAULT_EMAIL_TEMPLATE, encoding="utf-8")
        sys.exit(
            f"{EMAIL_TEMPLATE_FILE} didn't exist yet, so I created a starter "
            f"one for you - open it, edit the fixed text and the {{{{...}}}} "
            f"instructions to match your real email, then run this again."
        )
    email_template_text = EMAIL_TEMPLATE_FILE.read_text(encoding="utf-8")
    personas = _load_personas()
    if personas:
        print(f"  loaded {len(personas)} persona template(s) from {PERSONA_TEMPLATES_FILE.name}: "
              + ", ".join(personas.keys()))
    else:
        # This used to be silent - a run would just proceed with an empty
        # personas dict and every personal_emails lead would quietly end up
        # "no persona matched" with no clue why, even though the free
        # _could_match_any_persona() gate (a fixed keyword list, independent
        # of this file) would still happily wave every lead through first.
        # Loud on purpose now: this is almost always either
        # PERSONA_TEMPLATES_FILE.name not existing/misspelled, or a "---"
        # section whose label line doesn't end with ":" so every block in
        # the file got silently skipped by _load_personas().
        print(f"  WARNING: 0 persona templates loaded from {PERSONA_TEMPLATES_FILE.name} "
              f"({'file not found' if not PERSONA_TEMPLATES_FILE.exists() else 'file exists but nothing in it parsed as a valid persona'}) - "
              f"EVERY personal_emails lead this run will end up needing your attention with "
              f"\"no persona matched\", regardless of how good a fit they actually are, because "
              f"there's nothing to match them against. Check that {PERSONA_TEMPLATES_FILE.name} "
              f"exists next to outreach_bot.py and that each persona section starts with a label "
              f"line ending in ':' right after a '---' separator.")

    _report(progress, "starting", 0, 0, "", "connecting")
    client = get_gemini_client()

    _report(progress, "starting", 0, 0, "", "loading")
    wb, ws, header, rows = load_leads()
    state = load_state()

    # 1) new leads -> liveness check -> first email
    #
    # Guard against the bot's own sending address (GMAIL_USER) ever ending
    # up as a "lead" - by accident (copy-paste, a test row left in) or
    # otherwise. Emailing yourself isn't just pointless: Gmail delivers a
    # self-addressed email into your own inbox, so the bot's own outbound
    # cold email would then look like an incoming reply FROM that same
    # address, and it could end up trying to reply to itself.
    # Repair leads that an older version marked "dead" just because OUR DNS
    # lookup timed out (not because anything was wrong with the address) -
    # put them back in line so they get a real check this time.
    for r in rows:
        notes = (r.get("notes") or "").lower()
        if (r.get("status") or "").strip().lower() == "dead" and \
                notes.startswith("liveness check: no mx record") and \
                ("timed out" in notes or "lifetime expired" in notes or "nonameservers" in notes):
            update_lead(ws, header, r["_row"], status="new",
                        notes="re-queued: was marked dead only because a DNS lookup timed out")
            r["status"] = "new"
            print(f"  RE-QUEUED {r['email']} - was wrongly marked dead by a DNS timeout")

    self_addr = _clean_email(GMAIL_USER)
    for r in rows:
        if self_addr and _clean_email(r.get("email")) == self_addr and \
                (r.get("status") or "new").strip().lower() == "new":
            update_lead(ws, header, r["_row"], status="dead",
                        notes="this is the bot's own sending address (GMAIL_USER) - skipped, not emailing itself")
            print(f"  SKIP (self) {r['email']}: this is the bot's own GMAIL_USER address")

    # Backfill company_research for any lead that has a website on file but
    # somehow never got a research summary - most commonly a lead whose
    # FIRST email already went out/got drafted on a run where the scrape
    # happened to fail (site was briefly down, a network blip, etc.).
    # fetch_website_text()/research_company() never raise on that kind of
    # failure - they just leave company_research blank for "this time" -
    # but "this time" used to BE the only time: once a lead moves past
    # "new" it's never looked at again by the new-leads section below, so
    # a transient failure meant that lead's company_research stayed empty
    # forever. This runs every time, independent of status (skipping only
    # the handful of truly-done statuses), so a blank one keeps getting
    # retried on later runs until it actually succeeds - it just can't
    # rewrite an email that already went out, but it does fill in the
    # table/dashboard so the research is there for anything after that
    # (replies, a human glancing at the row, etc).
    MAX_RESEARCH_BACKFILL_PER_RUN = int(os.getenv("MAX_RESEARCH_BACKFILL_PER_RUN", "5"))
    backfilled = 0
    for r in rows:
        if backfilled >= MAX_RESEARCH_BACKFILL_PER_RUN:
            break
        if (r.get("status") or "").strip().lower() in ("dead", "rejected", "booked"):
            continue
        if (r.get("company_research") or "").strip():
            continue
        # Wrapped for the same reason as the new-leads loop below: one row's
        # scrape/summarize hiccup (a network blip, a Gemini error, a site
        # that's briefly down) should never abort the whole run - it should
        # just skip that row and retry it on a future run, same as before
        # this backfill pass existed at all.
        try:
            is_company_row = r.get("_table") == SUPABASE_COMPANY_TABLE
            raw_site = r.get("website") if is_company_row else r.get("company_website")
            site = _normalize_website(raw_site)
            if not site and "@" in (r.get("email") or ""):
                site = _normalize_website(r["email"].split("@", 1)[1])
            if not site:
                continue
            _report(progress, "starting", backfilled, MAX_RESEARCH_BACKFILL_PER_RUN,
                    r.get("company") or r.get("email"), "backfill_research")
            website_text = fetch_website_text(site)
            if not website_text:
                continue
            summary = research_company(client, r.get("company") or "", website_text)
            if not summary:
                continue
            if is_company_row:
                update_lead(ws, header, r["_row"], website=site, company_research=summary)
            else:
                update_lead(ws, header, r["_row"], company_website=site, company_research=summary)
            r["company_research"] = summary
            backfilled += 1
            print(f"  backfilled company research for {r.get('company') or r['email']} ({site}): "
                  f"{summary[:100]}...")
        except Exception as e:
            print(f"  ERROR backfilling research for {r.get('email')}: {type(e).__name__}: {e} - "
                  f"skipping it, will retry on a future run.")
            continue

    # Backfill role_category for any personal_emails contact that doesn't
    # have one yet (added before this feature existed, or added since but
    # not yet processed) - so find_better_contact_at_company() below has as
    # complete a picture as possible of who's already known to be a
    # security/ops/CEO contact at a given company, not just whoever
    # happens to be in THIS run's fresh "new" batch. Classification is
    # cheap (mostly free keyword matches), so this is generous by default -
    # see MAX_ROLE_BACKFILL_PER_RUN near the top of this file.
    role_backfilled = 0
    for r in rows:
        if role_backfilled >= MAX_ROLE_BACKFILL_PER_RUN:
            break
        if r.get("_table") != SUPABASE_PERSON_TABLE:
            continue
        if (r.get("status") or "").strip().lower() in ("dead", "rejected"):
            continue
        if (r.get("role_category") or "").strip():
            continue
        try:
            _report(progress, "starting", role_backfilled, MAX_ROLE_BACKFILL_PER_RUN,
                    r.get("contact_name") or r.get("email"), "backfill_role")
            role = classify_role(client, r.get("job_title") or "")
            try:
                update_lead(ws, header, r["_row"], role_category=role["category"],
                            role_confidence=role["confidence"], role_method=role["method"])
            except Exception as e:
                print(f"  couldn't persist role_category for {r.get('email')} ({e}) - have "
                      f"you run the role_category/role_confidence/role_method ALTER TABLE "
                      f"in supabase_schema.sql yet? Using it for THIS run only, in memory.")
            r["role_category"] = role["category"]
            r["role_confidence"] = role["confidence"]
            r["role_method"] = role["method"]
            role_backfilled += 1
            print(f"  classified {r.get('contact_name') or r['email']} (job title: "
                  f"{r.get('job_title') or '(not given)'}) as role_category={role['category']!r} "
                  f"(confidence {role['confidence']}, via {role['method']})")
        except Exception as e:
            print(f"  ERROR classifying role for {r.get('email')}: {type(e).__name__}: {e} - "
                  f"skipping it, will retry on a future run.")
            continue

    _report(progress, "starting", 0, 0, "", "checking_new")
    # Still one company PER RUN (a dead lead no longer ends the run
    # empty-handed - the loop below works through up to MAX_NEW_TRIES_PER_TABLE
    # candidates, oldest first, until it lands one successful draft/send).
    #
    # personal_emails is handled differently now (see the batch classification
    # pass right below): instead of picking candidates one at a time and
    # discovering - AFTER a liveness check, a website scrape, and a full
    # compose call - that a given lead's persona doesn't fit, the WHOLE batch
    # of eligible new people is classified together in ONE Gemini call
    # up front (classify_personas_batch()). Only the survivors of that call
    # (a real persona match) go on to a liveness check and a compose call -
    # and EVERY survivor gets processed this run, not just the first one,
    # since the whole point of batching is to stop burning a run per person.
    MAX_NEW_TRIES_PER_TABLE = int(os.getenv("MAX_NEW_TRIES_PER_TABLE", "10"))
    # How many "new" personal_emails rows to feed into ONE batch
    # classification call per run. Generous by default - classification is
    # now a single call regardless of batch size, so there's little reason
    # to keep it as small as MAX_NEW_TRIES_PER_TABLE used to be.
    PERSON_BATCH_SIZE = int(os.getenv("PERSON_BATCH_SIZE", "40"))

    def _eligible_new(table_name):
        return [
            r for r in rows
            if r.get("_table") == table_name
            and (r.get("status") or "new").strip().lower() == "new"
            and not (self_addr and _clean_email(r.get("email")) == self_addr)
        ]

    company_candidates = [(r, True) for r in _eligible_new(SUPABASE_COMPANY_TABLE)[:MAX_NEW_TRIES_PER_TABLE]]

    # ---- personal_emails: batch persona/role classification, up front ----
    person_eligible = _eligible_new(SUPABASE_PERSON_TABLE)[:PERSON_BATCH_SIZE]
    person_batch_candidates = []  # rows that pass the free gate below
    for r in person_eligible:
        job_title_on_file = (r.get("job_title") or "").strip()
        industry_on_file = (r.get("industry") or "").strip()
        # Free, no-API-call gate (unchanged) - a title/industry combo that
        # plainly can't match ANY persona is flagged right away, without
        # spending a slot in the batch classification call on it at all.
        if not _could_match_any_persona(job_title_on_file, industry_on_file):
            reason = (f"job title: {job_title_on_file or '(not given)'}, "
                      f"industry: {industry_on_file or '(not given)'}")
            print(f"  {r.get('contact_name') or r['email']} - {reason} - no persona in "
                  f"{PERSONA_TEMPLATES_FILE.name} could plausibly cover this, skipping "
                  f"the batch classification call, the website scrape, and the compose "
                  f"call entirely (pure waste either way).")
            note = f"no persona could plausibly match ({reason}) - needs a human pick"
            update_lead(ws, header, r["_row"], status="needs_attention", notes=note)
            log_activity("ALERT (no persona matched)", r.get("company") or "",
                         r["email"], body=note)
            log_no_persona_lead(r.get("company"), r.get("contact_name"), r["email"],
                                 job_title_on_file, reason)
            print(f"  {r['email']} needs your attention - {reason} - visible on the "
                  f"dashboard, no alert email sent.")
            continue
        person_batch_candidates.append(r)

    person_ready = []  # rows that also got a real persona match - these go on
    if person_batch_candidates:
        batch_input = [
            {
                "key": r["email"],
                "job_title": r.get("job_title") or "",
                "industry": r.get("industry") or "",
                "contact_name": r.get("contact_name") or "",
                "company": r.get("company") or "",
                # Both free to include - already sitting on the row from a
                # previous run/enrichment, no new scrape or API call spent
                # to get them. Genuinely NEW research (a website that
                # hasn't been scraped yet) is deliberately NOT fetched here
                # just to feed this classification call - that would bring
                # back exactly the kind of per-lead cost this batch call
                # exists to avoid; it still happens later, but only for
                # whoever this call actually matches to a persona.
                "linkedin_info": r.get("linkedin_info") or "",
                "company_research": r.get("company_research") or "",
            }
            for r in person_batch_candidates
        ]
        _report(progress, "starting", 0, len(person_batch_candidates), "", "role_classify")
        classified = classify_personas_batch(client, personas, batch_input, icp_text)
        for r in person_batch_candidates:
            result = classified.get(r["email"]) or {
                "persona": None, "role_category": "other", "confidence": 0.0,
            }
            try:
                update_lead(ws, header, r["_row"], role_category=result["role_category"],
                            role_confidence=result["confidence"], role_method="ai_batch",
                            matched_persona=result["persona"] or "NO_FITTING_ROLE")
            except Exception as e:
                print(f"  couldn't persist batch classification for {r['email']} ({e}) - "
                      f"have you run the matched_persona ALTER TABLE (see "
                      f"add_matched_persona_column.sql)? Using it for THIS run only, in memory.")
            r["role_category"] = result["role_category"]
            r["role_confidence"] = result["confidence"]
            r["_matched_persona"] = result["persona"]
            if not result["persona"]:
                title_for_note = (r.get("job_title") or "").strip() or "(not given)"
                note = f"no persona matched (job title: {title_for_note}) - needs a human pick"
                update_lead(ws, header, r["_row"], status="needs_attention", notes=note)
                log_activity("ALERT (no persona matched)", r.get("company") or "",
                             r["email"], body=note)
                log_no_persona_lead(r.get("company"), r.get("contact_name"), r["email"],
                                     title_for_note, "no persona matched")
                print(f"  {r['email']} needs your attention - no persona matched (job title: "
                      f"{title_for_note}) - visible on the dashboard, no alert email sent.")
                continue
            print(f"  batch-classified {r.get('contact_name') or r['email']} (job title: "
                  f"{r.get('job_title') or '(not given)'}) -> persona={result['persona']!r}, "
                  f"role_category={result['role_category']!r} (confidence {result['confidence']})")
            person_ready.append(r)

    batch = company_candidates + [(r, False) for r in person_ready]
    if not batch:
        print("  nothing new in company_emails or personal_emails this run.")

    sent_count = 0
    drafted_count = 0
    company_done = False
    total_leads = len(batch)
    # personal_emails leads that passed the liveness check and got a real
    # persona match, queued up here instead of composed inline - see the
    # "else:" branch below (personal_emails, non-HR). Drafted together,
    # a handful at a time, in ONE compose_person_emails_batch() call per
    # chunk, right after this loop finishes.
    live_persons_for_compose = []
    progress("leads", 0, total_leads, "")
    for i, (r, is_company) in enumerate(batch, start=1):
        # Already got this run's one company draft/send from an earlier,
        # live candidate - don't also try this table's remaining backup
        # candidates, they're only here in case an earlier one turned out
        # dead/unusable. personal_emails has no such cap any more - every
        # row that reached `person_ready` above (live-checked below) gets
        # its own draft/send this run, since the whole batch was already
        # confirmed persona-worthy before it ever got here.
        if is_company and company_done:
            continue

        # Wrapped so ONE candidate's problem (a Gemini/network hiccup - the
        # kind of thing that shows up as EVERY site failing DNS resolution at
        # once, including domains that obviously work like gmail.com, is your
        # whole machine's network dropping out for a moment, not each site
        # being individually broken) can never take the whole run down with
        # it - same idea as the replies loop below. Before this, an exception
        # anywhere in here (liveness check, website scrape, the Gemini call
        # itself) would propagate all the way up through _run_once() and
        # abort the ENTIRE scan - ui_app.py catches that at the very top and
        # reports "scan failed: ..." with nothing else in this run completing,
        # even a company/person that had nothing to do with the failure. Now a
        # candidate that blows up this way is just left as "new" (never
        # marked dead - it wasn't actually confirmed dead, we just don't know)
        # and the loop moves on to the next candidate/table, same as a dead or
        # unmatched one.
        try:
            lead_label = r.get("company") or r.get("email") or r.get("contact_name") or ""
            _report(progress, "leads", i - 1, total_leads, lead_label, "liveness")
            live, reason = is_probably_live(r["email"])
            if not live:
                update_lead(ws, header, r["_row"], status="dead", notes=f"liveness check: {reason}")
                log_activity("DEAD (liveness check failed)", r["company"], r["email"],
                             body=f"liveness check: {reason}")
                print(f"SKIP (not live) {r['email']}: {reason} - trying the next "
                      f"{'company' if is_company else 'person'} in line instead.")
                progress("leads", i, total_leads, lead_label)
                continue

            # Deterministic check (no API call): does this address belong to a
            # named person, or a shared/role inbox (info@, support@...)? Same
            # answer every time for the same address, logged here and passed
            # straight into the composer instead of leaving it to be inferred.
            recipient = classify_recipient(r["email"])
            if recipient["kind"] == "generic_inbox":
                print(f"  recipient check: {r['email']} - {recipient['reason']}")
            lead_notes = recipient["reason"] if recipient["kind"] == "generic_inbox" else ""

            if is_company:
                # company_emails: research the company from its own website
                # (once - skip if we already have it stored), then ALWAYS
                # compose from EMAIL_TEMPLATE_FILE - no persona logic at all
                # for this table.
                research = (r.get("company_research") or "").strip()
                if not research:
                    website = _normalize_website(r.get("website"))
                    if not website and "@" in (r["email"] or ""):
                        website = _normalize_website(r["email"].split("@", 1)[1])
                    if website:
                        _report(progress, "leads", i - 1, total_leads, lead_label, "website")
                        website_text = fetch_website_text(website)
                        if website_text:
                            research = research_company(client, r["company"], website_text)
                            if research:
                                update_lead(ws, header, r["_row"], website=website,
                                            company_research=research)
                                print(f"  researched {r['company']} ({website}): {research[:100]}...")
                        # if the fetch/summary comes up empty, leave company_research
                        # blank so it's retried next run rather than giving up for good

                _report(progress, "leads", i - 1, total_leads, lead_label, "compose")
                draft = compose_first_email_from_template(
                    client, icp_text, email_template_text, r["company"], r["email"],
                    research=research, recipient_kind=recipient["kind"],
                )
            else:
                # personal_emails: the primary enrichment source is whatever's
                # already sitting in linkedin_info (populated by you running
                # enrich_from_linkedin.py by hand; this run just reads it).
                # ON TOP of that, if this person has a company_website on file,
                # scrape+summarize it too (same research_company() used for
                # company_emails, cached into company_research so it's only
                # scraped once) - both are handed to the composer together.
                # ALWAYS composed from personal_email_templete.txt - never
                # EMAIL_TEMPLATE_FILE for this table.
                research = (r.get("linkedin_info") or "").strip()
                if not research:
                    print(f"  {r.get('contact_name') or r['email']} has no linkedin_info yet - "
                          f"run enrich_from_linkedin.py first for a real persona pick, or this "
                          f"will likely fall through to needing your attention.")

                # Persona + role_category were ALREADY decided for this
                # person by the batch classification pass above (see
                # classify_personas_batch()) - r["_matched_persona"] is a
                # real template label (never blank/None here, since only
                # `person_ready` rows - the ones that got a real match -
                # ever reach this loop at all), and r["role_category"]/
                # r["role_confidence"] are what that same call decided.
                # Nothing here needs to re-scrape a persona decision or
                # spend another classify_role() AI call on it.
                job_title_on_file = (r.get("job_title") or "").strip()
                industry_on_file = (r.get("industry") or "").strip()
                # No live website fetch here any more - deliberately. A
                # website scrape used to happen inline, right here, one lead
                # at a time; now it's prefetch_raw_scrapes()'s job (its own
                # CLI command, run ahead of time, see add_raw_scrape_column.sql)
                # so THIS run never blocks on a live fetch. We just read
                # whatever's already cached - a fresh raw_scrape (preferred -
                # compose_person_emails_batch() reads/summarizes it itself) or
                # an older already-summarized company_research (fallback, from
                # before this feature existed, or from the company_emails-style
                # research still done elsewhere) - and if neither is on file
                # yet, this lead's email still gets composed, just without a
                # company-research signal, same as it always could.
                company_research = (r.get("company_research") or "").strip()
                raw_scrape = (r.get("raw_scrape") or "").strip()
                matched_persona = r.get("_matched_persona")

                role_category = effective_role_category(r.get("role_category"), r.get("role_confidence"))

                if role_category == "hr":
                    # HR is (almost) never the actual decision-maker on
                    # security cameras - see find_better_contact_at_company()/
                    # compose_hr_referral_email() near classify_role() above
                    # for the full rationale. Path A: if we already know of
                    # a security/ops/CEO contact at this SAME company
                    # (anywhere in rows - already-loaded, either table),
                    # that contact gets its own normal turn in this run (or
                    # a future one) on its own merit - don't ALSO pitch HR,
                    # skip this candidate and try the next one in line.
                    better = find_better_contact_at_company(rows, r.get("company") or "", r["email"])
                    if better:
                        print(f"  SKIP (HR routing) {r['email']}: {better.get('contact_name') or better['email']} "
                              f"at {r.get('company')} is already a better contact "
                              f"(role_category={better.get('role_category')!r}) - not pitching HR directly, "
                              f"trying the next person in line instead.")
                        progress("leads", i, total_leads, lead_label)
                        continue
                    # Path B: no better contact on file - a short referral-ask
                    # instead of the normal persona pitch (HR never gets that).
                    _report(progress, "leads", i - 1, total_leads, lead_label, "compose")
                    draft = compose_hr_referral_email(
                        client, icp_text, r.get("company") or "", r["email"],
                        job_title=r.get("job_title") or "", contact_name=r.get("contact_name") or "",
                    )
                    if draft is None:
                        note = (f"{lead_notes} / " if lead_notes else "") + \
                            "HR referral-ask email failed validation twice - needs a human look"
                        update_lead(ws, header, r["_row"], status="needs_attention", notes=note)
                        log_activity("ALERT (HR referral-ask failed)", r.get("company") or "",
                                     r["email"], body=note)
                        print(f"  ALERT: {r['email']} needs your attention - HR referral-ask "
                              f"email failed validation - trying the next person in line instead.")
                        progress("leads", i, total_leads, lead_label)
                        continue
                else:
                    # Don't compose yet - queue this lead so its email gets
                    # drafted together with a handful of other survivors in
                    # ONE shared compose_person_emails_batch() call after this
                    # loop finishes, instead of its own per-lead Gemini call
                    # right here (see that function's docstring). r["_row"]
                    # is kept on the queued entry so the send/draft handling
                    # below (after the batch compose call returns) can still
                    # update_lead()/log_activity() this exact row.
                    live_persons_for_compose.append({
                        "row": r,
                        "key": r["email"],
                        "company": r.get("company") or "",
                        "email_addr": r["email"],
                        "job_title": r.get("job_title") or "",
                        "contact_name": r.get("contact_name") or "",
                        "research": research,
                        "company_research": company_research,
                        "raw_scrape": raw_scrape,
                        "recipient_kind": recipient["kind"],
                        "role_category": role_category,
                        "industry": industry_on_file,
                        "forced_persona": matched_persona,
                        "lead_notes": lead_notes,
                        "lead_label": lead_label,
                    })
                    progress("leads", i, total_leads, lead_label)
                    continue
                if draft is None:
                    title_for_note = (r.get("job_title") or "").strip() or "(not given)"
                    # The batch pass already confirmed a persona fit for this
                    # person (that's the only way they got this far) - a
                    # None here means compose_person_email() itself couldn't
                    # produce a version that passed style validation even
                    # after one retry (e.g. it kept using a dash, or kept
                    # dropping the mandatory closing line), NOT a persona
                    # mismatch. Worded accordingly so it's not confused with
                    # the earlier, separate "no persona could plausibly
                    # match"/"no persona matched" flags above.
                    note = (f"{lead_notes} / " if lead_notes else "") + \
                        f"matched persona {matched_persona!r} but the email failed style " \
                        f"validation twice (job title: {title_for_note}) - needs a human look"
                    update_lead(ws, header, r["_row"], status="needs_attention", notes=note)
                    log_activity("ALERT (persona email failed validation)", r.get("company") or "",
                                 r["email"], body=note)
                    print(f"  {r['email']} needs your attention - matched persona "
                          f"{matched_persona!r} but the email failed style validation twice "
                          f"(job title: {title_for_note}) - visible on the dashboard, no alert "
                          f"email sent.")
                    progress("leads", i, total_leads, lead_label)
                    continue

            if is_company:
                company_done = True
            # personal_emails has no equivalent "done" flag any more - every
            # row in `person_ready` is meant to get its own draft/send this
            # run (see the batch classification pass above).

            if REVIEW_MODE:
                update_lead(ws, header, r["_row"], status="draft_pending",
                            draft_subject=draft["subject"], draft_body=draft["body"],
                            notes=lead_notes)
                log_activity("DRAFT (awaiting your approval)", r["company"], r["email"],
                             draft["subject"], draft["body"])
                print(f"DRAFTED (not sent, awaiting approval) -> {r['email']}")
                drafted_count += 1
            else:
                _report(progress, "leads", i - 1, total_leads, lead_label, "sending")
                msg_id = send_one(r["email"], draft["subject"], draft["body"])
                update_lead(ws, header, r["_row"], status="contacted", message_id=msg_id, notes=lead_notes)
                log_activity("SENT first email", r["company"], r["email"],
                             draft["subject"], draft["body"])
                print(f"SENT first email -> {r['email']}")
                sent_count += 1
            progress("leads", i, total_leads, lead_label)
            time.sleep(2)
        except Exception as e:
            print(f"  ERROR processing {r.get('email')}: {type(e).__name__}: {e} - "
                  f"leaving it as \"new\" and trying the next "
                  f"{'company' if is_company else 'person'} in line instead.")
            log_activity("ERROR processing new lead", r.get('company') or '', r.get('email') or '',
                         body=f"{type(e).__name__}: {e}")
            progress("leads", i, total_leads, lead_label)
            continue

    # ---- personal_emails: batch-compose everyone queued above ----
    # How many queued personal_emails leads go into ONE
    # compose_person_emails_batch() call. Kept modest (not PERSON_BATCH_SIZE's
    # 40) - the user asked for "a couple of people in one run", and a smaller
    # chunk also keeps a single bad/violating batch response from holding up
    # a large share of the run's drafts at once.
    COMPOSE_BATCH_SIZE = int(os.getenv("COMPOSE_BATCH_SIZE", "5"))
    total_queued = len(live_persons_for_compose)
    for start in range(0, total_queued, COMPOSE_BATCH_SIZE):
        chunk = live_persons_for_compose[start:start + COMPOSE_BATCH_SIZE]
        chunk_label = ", ".join(p["lead_label"] for p in chunk)
        _report(progress, "leads", total_leads, total_leads, chunk_label, "compose_batch")
        try:
            results = compose_person_emails_batch(client, icp_text, personas, chunk)
        except Exception as e:
            print(f"  ERROR batch-composing {len(chunk)} lead(s): {type(e).__name__}: {e} - "
                  f"leaving all of them as \"new\", trying the next chunk instead.")
            log_activity("ERROR batch-composing personal_emails leads", "", "",
                         body=f"{type(e).__name__}: {e}")
            continue

        for p in chunk:
            r = p["row"]
            try:
                draft = results.get(p["key"])
                if draft is None:
                    title_for_note = (p["job_title"] or "").strip() or "(not given)"
                    note = (f"{p['lead_notes']} / " if p["lead_notes"] else "") + \
                        f"matched persona {p['forced_persona']!r} but the batch email " \
                        f"failed style validation (job title: {title_for_note}) - needs a human look"
                    update_lead(ws, header, r["_row"], status="needs_attention", notes=note)
                    log_activity("ALERT (persona email failed validation)", r.get("company") or "",
                                 r["email"], body=note)
                    print(f"  {r['email']} needs your attention - matched persona "
                          f"{p['forced_persona']!r} but the batch email failed style validation "
                          f"(job title: {title_for_note}) - visible on the dashboard, no alert "
                          f"email sent.")
                    continue

                if REVIEW_MODE:
                    update_lead(ws, header, r["_row"], status="draft_pending",
                                draft_subject=draft["subject"], draft_body=draft["body"],
                                notes=p["lead_notes"])
                    log_activity("DRAFT (awaiting your approval)", r["company"], r["email"],
                                 draft["subject"], draft["body"])
                    print(f"DRAFTED (not sent, awaiting approval) -> {r['email']}")
                    drafted_count += 1
                else:
                    msg_id = send_one(r["email"], draft["subject"], draft["body"])
                    update_lead(ws, header, r["_row"], status="contacted", message_id=msg_id,
                                notes=p["lead_notes"])
                    log_activity("SENT first email", r["company"], r["email"],
                                 draft["subject"], draft["body"])
                    print(f"SENT first email -> {r['email']}")
                    sent_count += 1
                time.sleep(2)
            except Exception as e:
                print(f"  ERROR processing {r.get('email')}: {type(e).__name__}: {e} - "
                      f"leaving it as \"new\", trying the next person instead.")
                log_activity("ERROR processing new lead", r.get('company') or '', r.get('email') or '',
                             body=f"{type(e).__name__}: {e}")
                continue
    if total_queued:
        progress("leads", total_leads, total_leads, "")
    save_leads(wb)

    # 2) replies -> reply, and handle meeting scheduling
    progress("inbox_check", 0, 0, "")
    wb, ws, header, rows = load_leads()  # reload after status updates above
    replies = check_inbox_for_replies(rows, state)
    save_state(state)

    total_replies = len(replies)
    progress("replies", 0, total_replies, "")
    for i, rep in enumerate(replies, start=1):
        lead_state = state.setdefault("leads", {}).setdefault(rep["email"], {})
        offered = lead_state.get("offered_slots", [])
        reply_label = rep.get("company") or rep.get("email") or ""

        log_activity("REPLY RECEIVED", rep["company"], rep["email"],
                     rep.get("subject", ""), rep["message"])

        # Everything below is wrapped so ONE lead's problem (a Gemini
        # hiccup, a calendar error that isn't CalendarNotConfigured, a
        # network blip) can never take the whole run down with it and lose
        # every other lead's progress along the way - it just gets logged
        # and skipped, and the run moves on.
        _report(progress, "replies", i - 1, total_replies, reply_label, "reading")
        try:
            _handle_reply(client, icp_text, ws, header, rep, lead_state, offered,
                           progress=progress, idx=i - 1, total=total_replies, label=reply_label)
        except Exception as e:
            print(f"  ERROR handling reply from {rep['email']}: {type(e).__name__}: {e} "
                  f"- skipping this one, the rest of the run continues.")
            log_activity("ERROR handling reply", rep["company"], rep["email"],
                         "", f"{type(e).__name__}: {e}")
        progress("replies", i, total_replies, reply_label)

    save_leads(wb)  # persist everything the replies loop just did before reloading below

    # 3) Calendly: ask who's actually booked a slot since we last checked,
    # and mark those leads "booked". Only does anything if CALENDLY_API_TOKEN
    # is set in .env - a no-op (and no progress step shown) otherwise, same
    # as any other optional integration in this script.
    if CALENDLY_API_TOKEN:
        _report(progress, "calendly", total_replies, total_replies, "", "calendly_poll")
        wb, ws, header, rows = load_leads()  # fresh copy to write booked-status onto
        newly_booked = poll_calendly_bookings(ws, header, rows, state)
        if newly_booked:
            save_leads(wb)
            print(f"Calendly: {newly_booked} lead(s) newly marked booked.")

    # 4) Learning loop: distill lessons from any leads that just booked or
    # clearly declined, so future emails (this run's and onward) benefit
    # from what actually happened with real people.
    if LEARNING_ENABLED:
        _report(progress, "learning", total_replies, total_replies, "", "learning")
        _, _, _, rows_now = load_leads()  # fresh - picks up any Calendly-poll booked updates above
        newly_learned = run_learning_pass(client, rows_now, state)
        if newly_learned:
            print(f"Learning: added {newly_learned} new rule(s) to lessons.txt.")

    # Website prefetching for a FUTURE run's personal_emails leads happens
    # in run_prefetch_after_run() - deliberately called by cmd_run()/
    # ui_app.py's do_scan() AFTER this function has returned and its own
    # "done"/completion notice has already gone out, not from in here, so
    # prefetching for next time never delays the notice that THIS run
    # finished. See that function's docstring.

    save_state(state)
    progress("done", total_replies, total_replies, "")
    print(f"Done. {sent_count} sent, {drafted_count} drafted for your approval, "
          f"{len(replies)} replies handled.")
    if drafted_count:
        print(f"-> Review drafts in Supabase (status=draft_pending) or "
              f"{LOG_FILE.name}, then run: python outreach_bot.py approve")


def cmd_approve(args):
    with LEADS_LOCK:
        sent = _approve_pending(only_email=args.only)
        print(f"Approved and sent {sent} draft(s).")


def _approve_pending(only_email=None):
    """Sends every draft_pending row (or just one email if given). Caller
    must hold LEADS_LOCK. Returns how many were sent."""
    wb, ws, header, rows = load_leads()
    pending = [r for r in rows if (r.get("status") or "").strip().lower() == "draft_pending"]
    if only_email:
        pending = [r for r in pending if r["email"].strip().lower() == only_email.strip().lower()]

    sent = 0
    for r in pending:
        subject = r.get("draft_subject") or ""
        body = r.get("draft_body") or ""
        if not subject or not body:
            print(f"SKIP {r['email']}: no draft content stored, odd - check the row by hand.")
            continue
        msg_id = send_one(r["email"], subject, body)
        update_lead(ws, header, r["_row"], status="contacted", message_id=msg_id,
                    draft_subject="", draft_body="")
        log_activity("SENT (approved draft)", r["company"], r["email"], subject, body)
        print(f"SENT -> {r['email']}")
        sent += 1
        time.sleep(2)

    save_leads(wb)
    return sent


def _reject_draft(email_addr):
    """Marks a draft_pending row as rejected instead of sending it. Caller
    must hold LEADS_LOCK. Returns True if a row was found and rejected."""
    wb, ws, header, rows = load_leads()
    found = False
    for r in rows:
        if r["email"].strip().lower() == email_addr.strip().lower() and \
                (r.get("status") or "").strip().lower() == "draft_pending":
            update_lead(ws, header, r["_row"], status="rejected",
                        draft_subject="", draft_body="")
            log_activity("DRAFT REJECTED (not sent)", r["company"], r["email"])
            found = True
            break
    if found:
        save_leads(wb)
    return found


def cmd_reject(args):
    with LEADS_LOCK:
        ok = _reject_draft(args.email)
    print("Rejected." if ok else f"No pending draft found for {args.email}")


def _dismiss_attention(email_addr, new_status="replied"):
    """Clears a needs_attention flag once you've dealt with it (by hand, in
    your real inbox, or you just decided it doesn't need anything further) -
    this is the "unpin"/"resolve" action behind the dashboard's flagged-
    email panel. Caller must hold LEADS_LOCK. Sets status to new_status
    (default "replied" - stays in STATUSES_CONTACTABLE, so the bot keeps
    watching for a future reply from this lead and handles it normally,
    same as any other ongoing conversation) and clears the notes field.
    Returns True if a matching needs_attention row was found."""
    wb, ws, header, rows = load_leads()
    found = False
    for r in rows:
        if r["email"].strip().lower() == email_addr.strip().lower() and \
                (r.get("status") or "").strip().lower() == "needs_attention":
            update_lead(ws, header, r["_row"], status=new_status, notes="")
            log_activity(f"ATTENTION DISMISSED (status -> {new_status})", r["company"], r["email"])
            found = True
            break
    if found:
        save_leads(wb)
    return found


def cmd_dismiss_attention(args):
    with LEADS_LOCK:
        ok = _dismiss_attention(args.email, new_status=args.status)
    print(f"Dismissed (status -> {args.status})." if ok else f"No flagged (needs_attention) lead found for {args.email}")


def get_open_slots(count=3):
    service = get_calendar_service()
    return _find_open_slots(service, count)


def book_slot(email_addr, company, start, end):
    service = get_calendar_service()
    event = {
        "summary": f"Intro call with {company}",
        "description": f"Booked automatically by the outreach bot with {company}.",
        "start": {"dateTime": start, "timeZone": os.getenv("TIMEZONE", "Asia/Jerusalem")},
        "end": {"dateTime": end, "timeZone": os.getenv("TIMEZONE", "Asia/Jerusalem")},
        "attendees": [{"email": email_addr}],
    }
    return service.events().insert(calendarId="primary", body=event, sendUpdates="all").execute()


# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("run")

    p_approve = sub.add_parser("approve")
    p_approve.add_argument("--only", default=None,
                            help="only approve/send the draft for this one email address")

    p_reject = sub.add_parser("reject")
    p_reject.add_argument("--email", required=True)

    p_dismiss = sub.add_parser("dismiss-attention")
    p_dismiss.add_argument("--email", required=True)
    p_dismiss.add_argument("--status", default="replied",
                            help="status to set once dismissed (default: replied)")

    sub.add_parser("pull")

    p_prefetch = sub.add_parser("prefetch-scrapes")
    p_prefetch.add_argument("--count", default=40,
                             help="how many personal_emails leads (without a raw_scrape "
                                  "yet) to fetch website text for, oldest first (default: 40)")

    p_send = sub.add_parser("send")
    p_send.add_argument("--file", required=True)

    p_slots = sub.add_parser("slots")
    p_slots.add_argument("--count", default=3)

    p_book = sub.add_parser("book")
    p_book.add_argument("--email", required=True)
    p_book.add_argument("--start", required=True)
    p_book.add_argument("--end", required=True)
    p_book.add_argument("--company", default="")
    p_book.add_argument("--subject", default="")

    p_recap = sub.add_parser("daily-recap")
    p_recap.add_argument("--date", default=None,
                          help="YYYY-MM-DD, defaults to today")
    p_recap.add_argument("--out", default=None,
                          help="output PDF path, defaults to daily_recap_<date>.pdf")

    args = parser.parse_args()
    {
        "run": cmd_run,
        "approve": cmd_approve,
        "reject": cmd_reject,
        "dismiss-attention": cmd_dismiss_attention,
        "pull": cmd_pull,
        "prefetch-scrapes": cmd_prefetch_scrapes,
        "send": cmd_send,
        "slots": cmd_slots,
        "book": cmd_book,
        "daily-recap": cmd_daily_recap,
    }[args.cmd](args)


if __name__ == "__main__":
    main()