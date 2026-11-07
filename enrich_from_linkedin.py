#!/usr/bin/env python3
"""
enrich_from_linkedin.py - for every row in the personal_emails table that
has a contact_name but no linkedin_info yet, searches LinkedIn for that
person (narrowed by company when you have one) and saves a short summary
of what it finds - headline, current title/company, location - into
linkedin_info. If job_title is currently blank, it's backfilled from
whatever title LinkedIn shows (helps the persona-template matching in
compose_person_email() pick the right template).

linkedin_info is the ONLY research source personal_emails rows use (there
is no website-scrape step for that table - that's company_emails' job) -
run this before a run picks up a given person, or their persona blanks
will have nothing to work from and _run_once() will flag them for your
attention instead of guessing. This script only ever touches
personal_emails - company_emails rows have no contact_name column, so
they never match the filter below.

Uses open-linkedin-api (github.com/EseToni/open-linkedin-api), which logs
in as YOUR real LinkedIn account and talks to LinkedIn's private internal
API - not an official, sanctioned way to get this data. Two things worth
being deliberate about before you run this regularly:
  - It's a real login to your own LinkedIn account, on a schedule you
    control. Use an account you're comfortable putting at risk of a
    restriction/challenge if LinkedIn's automation detection flags it -
    not the account you personally do outbound with day to day, if you can
    help it.
  - This script is deliberately NOT wired into outreach_bot.py's run loop
    (the one Task Scheduler/the dashboard fires every 20 minutes) - it's a
    separate, manually-triggered script, on purpose, so a LinkedIn scan
    only happens when you explicitly run it, not silently every 20 minutes
    forever.
It logs in once and caches the session in .linkedin_cookies/ next to this
script (reused on later runs instead of a fresh login every time), sleeps
several seconds between people, and stops the whole run immediately the
moment LinkedIn challenges the login or the session gets rejected - it
does not keep hammering a flagged account.

Setup:
    pip install open-linkedin-api
    add to .env:
        LINKEDIN_EMAIL=you@example.com
        LINKEDIN_PASSWORD=...
    linkedin_info is already a column on personal_emails in
    supabase_schema.sql - nothing extra to run for a fresh setup.

Usage:
    python enrich_from_linkedin.py            # up to 20 leads, see --limit
    python enrich_from_linkedin.py --limit 5
    python enrich_from_linkedin.py --email you@x.com    # re-check one lead
"""
import argparse
import random
import sys
import time
from pathlib import Path

try:
    from open_linkedin_api import Linkedin
    from open_linkedin_api.client import ChallengeException, UnauthorizedException
except ImportError:
    sys.exit(
        "This script needs open-linkedin-api - run:\n"
        "    pip install open-linkedin-api"
    )

sys.path.insert(0, str(Path(__file__).parent))
import outreach_bot as bot

LINKEDIN_EMAIL = (bot.os.getenv("LINKEDIN_EMAIL") or "").strip()
LINKEDIN_PASSWORD = (bot.os.getenv("LINKEDIN_PASSWORD") or "").strip()
COOKIES_DIR = bot.BASE_DIR / ".linkedin_cookies"

# Deliberately conservative - this is someone's real account, not a
# disposable API key. Slower than the library's own default pacing, and a
# hard stop per run so a bad match/misconfigured loop can't silently
# hammer LinkedIn for hours unattended.
MIN_DELAY_SECONDS = 5
MAX_DELAY_SECONDS = 11
DEFAULT_LIMIT = 20


def _login():
    if not LINKEDIN_EMAIL or not LINKEDIN_PASSWORD:
        sys.exit(
            "LINKEDIN_EMAIL / LINKEDIN_PASSWORD aren't set in .env - add both "
            "(the account you're willing to risk a challenge/restriction on, "
            "see the warning at the top of this file) and try again."
        )
    COOKIES_DIR.mkdir(exist_ok=True)
    try:
        return Linkedin(LINKEDIN_EMAIL, LINKEDIN_PASSWORD, cookies_dir=str(COOKIES_DIR))
    except ChallengeException:
        sys.exit(
            "LinkedIn challenged this login (a verification step it wants a "
            "real browser session for) - log into linkedin.com as this "
            "account by hand once, complete whatever it's asking for, then "
            "try again."
        )
    except UnauthorizedException:
        sys.exit("LinkedIn rejected these credentials - double check LINKEDIN_EMAIL/LINKEDIN_PASSWORD in .env.")


def _best_match(api, name, company, title):
    """Searches LinkedIn for `name`, trying the most specific filter
    combination first and loosening it step by step until something comes
    back - so a company/title that's slightly off (or, like a test lead's
    placeholder company, not real at all) doesn't just return zero, and a
    common name with nothing to narrow it doesn't just return the single
    most-followed "Bar Cohen" in Israel. Each step is tried only if the
    step before it found nothing.

    LinkedIn's own search ranking picks the order within whichever step
    matches; this doesn't second-guess it beyond that. Every save prints
    which filters actually matched and how many candidates came back, so a
    wrong match (or a match that only worked once company/title were
    dropped) is easy to spot and re-check by eye."""
    attempts = []
    if company and title:
        attempts.append(("name + company + title", {"keyword_company": company, "keyword_title": title}))
    if company:
        attempts.append(("name + company", {"keyword_company": company}))
    if title:
        attempts.append(("name + title", {"keyword_title": title}))
    attempts.append(("name only", {}))

    for label, extra_kwargs in attempts:
        kwargs = {"keywords": name, **extra_kwargs}
        try:
            results = api.search_people(**kwargs)
        except (ChallengeException, UnauthorizedException):
            raise
        except Exception as e:
            print(f"  search failed for {name!r} ({label}): {e}")
            continue
        results = [r for r in results if r.get("urn_id")]
        if results:
            if label != attempts[0][0]:
                print(f"  no match on the more specific filters - found {len(results)} on '{label}' instead")
            return results[0], len(results), label
    return None, 0, None


def _format_linkedin_info(match, profile, candidate_count, matched_on):
    public_id = profile.get("public_id") or ""
    headline = (profile.get("headline") or "").strip()
    location = (profile.get("locationName") or match.get("location") or "").strip()
    experience = profile.get("experience") or []
    current = experience[0] if experience else {}
    title = (current.get("title") or match.get("jobtitle") or "").strip()
    company_name = (
        (current.get("companyName") or current.get("company", {}).get("name"))
        if isinstance(current.get("company"), dict) else current.get("companyName")
    )
    company_name = (company_name or "").strip()

    lines = [f"found via LinkedIn search on {bot.datetime.now().strftime('%Y-%m-%d')}"]
    if public_id:
        lines.append(f"profile: https://www.linkedin.com/in/{public_id}/")
    if headline:
        lines.append(f"headline: {headline}")
    if title or company_name:
        lines.append(f"current: {title or '?'}" + (f" at {company_name}" if company_name else ""))
    if location:
        lines.append(f"location: {location}")
    lines.append(f"matched on: {matched_on}")
    if candidate_count > 1:
        lines.append(
            f"note: {candidate_count} people matched this search - this is the "
            f"top-ranked result, worth a quick manual check if the name is common"
        )
    return "\n".join(lines), title


def enrich_one(api, row):
    name = (row.get("contact_name") or "").strip()
    company = (row.get("company") or "").strip()
    title = (row.get("job_title") or "").strip()
    match, candidate_count, matched_on = _best_match(api, name, company, title)
    if not match:
        print(f"  no LinkedIn match for {name!r} ({company or 'no company'}, {title or 'no title'}) "
              f"- tried every filter combination down to the name alone")
        return False

    try:
        profile = api.get_profile(urn_id=match["urn_id"]) or {}
    except (ChallengeException, UnauthorizedException):
        raise
    except Exception as e:
        print(f"  couldn't load the matched profile for {name!r}: {e}")
        return False

    linkedin_info, found_title = _format_linkedin_info(match, profile, candidate_count, matched_on)
    updates = {"linkedin_info": linkedin_info}
    if found_title and not (row.get("job_title") or "").strip():
        updates["job_title"] = found_title  # only fills a BLANK job_title, never overwrites one you set

    return linkedin_info, updates


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                         help=f"max leads to look up this run (default {DEFAULT_LIMIT})")
    parser.add_argument("--email", default=None,
                         help="only re-check this one lead's email, even if it already has linkedin_info")
    args = parser.parse_args()

    with bot.LEADS_LOCK:
        client, ws, header, rows = bot.load_leads()

    candidates = [r for r in rows if (r.get("contact_name") or "").strip()]
    if args.email:
        target = bot._clean_email(args.email)
        candidates = [r for r in candidates if bot._clean_email(r.get("email")) == target]
        if not candidates:
            sys.exit(f"No lead with contact_name set found for {args.email}.")
    else:
        candidates = [r for r in candidates if not (r.get("linkedin_info") or "").strip()]

    if not candidates:
        print("Nothing to do - no leads with a contact_name are missing linkedin_info.")
        return
    candidates = candidates[:args.limit]
    print(f"Looking up {len(candidates)} lead(s) on LinkedIn "
          f"(logged in as {LINKEDIN_EMAIL})...")

    api = _login()

    done, skipped = 0, 0
    for i, row in enumerate(candidates, start=1):
        name = row.get("contact_name")
        print(f"[{i}/{len(candidates)}] {name} ({row.get('company') or 'no company'})")
        try:
            result = enrich_one(api, row)
        except (ChallengeException, UnauthorizedException) as e:
            print(
                f"\nLinkedIn challenged/rejected this session mid-run ({type(e).__name__}) "
                f"- stopping here rather than continuing on a flagged login. "
                f"{done} lead(s) were saved before this happened; re-run the script "
                f"later once you've logged into linkedin.com by hand and cleared "
                f"whatever it's asking for."
            )
            break
        except Exception as e:
            print(f"  unexpected error on {name!r}: {type(e).__name__}: {e} - skipping")
            skipped += 1
            continue

        if not result:
            skipped += 1
        else:
            linkedin_info, updates = result
            with bot.LEADS_LOCK:
                bot.update_lead(ws, header, row["_row"], **updates)
            print(f"  saved: {linkedin_info.splitlines()[1] if len(linkedin_info.splitlines()) > 1 else linkedin_info}")
            done += 1

        if i < len(candidates):
            time.sleep(random.uniform(MIN_DELAY_SECONDS, MAX_DELAY_SECONDS))

    print(f"\nDone. {done} lead(s) enriched, {skipped} skipped/not found.")


if __name__ == "__main__":
    main()