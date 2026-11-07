#!/usr/bin/env python3
"""
check_hyphen_emails.py - batch-checks a set of email addresses for a
hyphen ("-") anywhere in the address, and reports which ones to remove.

Rule: a hyphen anywhere in the email address fails the check (flagged for
removal). No hyphen -> passes.

Usage
-----
    python check_hyphen_emails.py
        No file given -> pulls every lead currently in Supabase (both the
        company_emails and personal_emails tables, same source ui_app.py's
        dashboard reads from) and checks all of their "email" values.

    python check_hyphen_emails.py some_emails.txt
        Checks the emails in a plain text file instead (one email per
        line; blank lines and lines starting with # are skipped). Also
        accepts a .csv with an "email" column.

Output
------
Prints a report to the terminal and writes hyphen_email_report.csv next
to this file (so you've got a record to hand off / review), with columns:
email, source, verdict, note.

Exit code is 1 if anything was flagged (so this can be wired into a CI/
batch step that should fail loudly), 0 if every email passed.
"""

import csv
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
REPORT_PATH = BASE_DIR / "hyphen_email_report.csv"


def _load_from_file(path):
    """Returns a list of (email, source) from a .txt (one per line) or a
    .csv with an "email" column."""
    path = Path(path)
    entries = []
    if path.suffix.lower() == ".csv":
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames or "email" not in [c.lower() for c in reader.fieldnames]:
                sys.exit(f"{path}: no 'email' column found in the CSV header.")
            email_col = next(c for c in reader.fieldnames if c.lower() == "email")
            for row in reader:
                e = (row.get(email_col) or "").strip()
                if e:
                    entries.append((e, path.name))
    else:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    entries.append((line, path.name))
    return entries


def _load_from_supabase():
    """Pulls every current lead from Supabase via outreach_bot.load_leads()
    - the exact same data source the dashboard shows - and returns
    (email, source) pairs, source being "company:<name>" or
    "person:<name> @ <company>" so a flagged row is easy to find."""
    import outreach_bot as bot

    _, _, _, rows = bot.load_leads()
    entries = []
    for row in rows:
        email = (row.get("email") or "").strip()
        if not email:
            continue
        if row.get("_table") == bot.SUPABASE_PERSON_TABLE:
            who = row.get("contact_name") or "(no name)"
            company = row.get("company") or "(no company)"
            source = f"person: {who} @ {company}"
        else:
            source = f"company: {row.get('company') or '(no company)'}"
        entries.append((email, source))
    return entries


def check_batch(entries):
    """entries: list of (email, source). Returns (flagged, passed), each a
    list of (email, source)."""
    flagged, passed = [], []
    for email, source in entries:
        if "-" in email:
            flagged.append((email, source))
        else:
            passed.append((email, source))
    return flagged, passed


def main():
    if len(sys.argv) > 1:
        entries = _load_from_file(sys.argv[1])
        print(f"Checking {len(entries)} email(s) from {sys.argv[1]} ...\n")
    else:
        print("No file given - pulling leads from Supabase ...\n")
        entries = _load_from_supabase()
        print(f"Checking {len(entries)} email(s) from Supabase ...\n")

    if not entries:
        print("Nothing to check.")
        return 0

    flagged, passed = check_batch(entries)

    if flagged:
        print(f"REMOVE - {len(flagged)} email(s) contain a hyphen:")
        for email, source in flagged:
            print(f"  [-] {email}   ({source})")
        print()

    print(f"PASS - {len(passed)} email(s) have no hyphen.")
    print(f"\n{len(flagged)} to remove, {len(passed)} passed, {len(entries)} checked total.")

    with open(REPORT_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["email", "source", "verdict", "note"])
        for email, source in flagged:
            writer.writerow([email, source, "REMOVE", "contains a hyphen"])
        for email, source in passed:
            writer.writerow([email, source, "PASS", ""])
    print(f"\nFull report written to {REPORT_PATH.name}")

    return 1 if flagged else 0


if __name__ == "__main__":
    sys.exit(main())
