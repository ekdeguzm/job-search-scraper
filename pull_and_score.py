#!/usr/bin/env python3
"""
Pulls active security/SWE-relevant postings from SimplifyJobs and HackedRico,
scores each against Emil's two resumes, and appends new (not-yet-seen) rows
to the "All Jobs" tab of the tracker sheet.

COMPLETELY FREE BY DEFAULT: scoring is done with keyword matching, no API
calls, no cost. If you ever add an ANTHROPIC_API_KEY repo secret, this script
automatically switches to Claude-based scoring instead -- no code change
needed, just add the secret.

Safe to re-run daily: skips any posting URL already in the sheet, and skips
anything below FIT_THRESHOLD so the sheet doesn't fill with noise.
"""

import os
import re
import json
from datetime import date

import requests
import gspread
from google.oauth2.service_account import Credentials

# ---- Config ----
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
GOOGLE_CREDS_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
USE_AI_SCORING = bool(ANTHROPIC_API_KEY)  # free keyword mode unless a key is set

FIT_THRESHOLD = 40      # don't add anything scored below this
BATCH_SIZE = 20         # jobs per Claude scoring call
MODEL = "claude-sonnet-5"

SIMPLIFY_INTERN = "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json"
SIMPLIFY_NEWGRAD = "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json"
HACKEDRICO = "https://raw.githubusercontent.com/HackedRico/2027-cyber-jobs/main/listings.json"

SEC_KW = re.compile(
    r'\bsecurity\b|appsec|app sec|infosec|cyber|vulnerabilit|pentest|'
    r'penetration test|\bsoc\b|threat intel|red team|\biam\b|identity.{0,15}access',
    re.I,
)
INFRA_KW = re.compile(
    r'cloud|infrastructure|platform engineer|site reliability|\bsre\b|devops|backend|terraform',
    re.I,
)
RELEVANT_TERMS = {"Summer 2027", "Fall 2026"}

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
RESUME_SECURITY = open(os.path.join(SCRIPT_DIR, "resume_security.txt")).read()
RESUME_SWE = open(os.path.join(SCRIPT_DIR, "resume_swe.txt")).read()


def fetch_json(url):
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    return r.json()


def collect_candidates():
    candidates = []

    for url in (SIMPLIFY_INTERN, SIMPLIFY_NEWGRAD):
        for x in fetch_json(url):
            if not x.get("active"):
                continue
            if not (set(x.get("terms", [])) & RELEVANT_TERMS):
                continue
            title = x.get("title", "")
            if not (SEC_KW.search(title) or INFRA_KW.search(title)):
                continue
            candidates.append({
                "source": "SimplifyJobs",
                "company": x.get("company_name", ""),
                "title": title,
                "location": ", ".join(x.get("locations", [])[:2]),
                "url": x.get("url", ""),
            })

    for x in fetch_json(HACKEDRICO):
        if x.get("closed"):
            continue
        candidates.append({
            "source": "HackedRico",
            "company": x.get("company", ""),
            "title": x.get("role", ""),
            "location": x.get("location", ""),
            "url": x.get("url", ""),
        })

    seen, deduped = set(), []
    for c in candidates:
        if not c["url"]:
            continue
        key = (c["company"].strip().lower(), c["title"].strip().lower())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(c)
    return deduped


def get_sheet():
    creds = Credentials.from_service_account_info(
        json.loads(GOOGLE_CREDS_JSON),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    gc = gspread.authorize(creds)
    return gc.open_by_key(SPREADSHEET_ID).worksheet("All Jobs")


def already_tracked(sheet):
    urls = sheet.col_values(9)  # column I: JD Link
    return {u.strip() for u in urls if u.strip()}


def keyword_score(job):
    """Free fallback scorer: title keywords only, no API call, no cost.
    Less precise than AI scoring -- e.g. it can't tell 'SoC' (chip design)
    apart from 'SOC' (security operations center) the way Claude can."""
    title = job["title"]
    sec_hits = len(SEC_KW.findall(title))
    infra_hits = len(INFRA_KW.findall(title))

    if sec_hits and infra_hits:
        return {"fit_score": 80, "best_resume": "Security",
                "reason": "Title matches both security and cloud/infra keywords"}
    if sec_hits:
        return {"fit_score": 75, "best_resume": "Security",
                "reason": "Title matches security keywords"}
    if infra_hits:
        return {"fit_score": 55, "best_resume": "SWE / Infra",
                "reason": "Title matches cloud/infra keywords"}
    return {"fit_score": 30, "best_resume": "Security",
            "reason": "No strong keyword match"}


def score_batch(client, batch):
    jobs_desc = "\n".join(
        f"{i+1}. {j['title']} at {j['company']} ({j['location']})"
        for i, j in enumerate(batch)
    )
    prompt = f"""You're screening job postings for a candidate against two resume versions.

SECURITY RESUME:
{RESUME_SECURITY}

SWE / INFRA RESUME:
{RESUME_SWE}

JOBS TO SCORE:
{jobs_desc}

For each job return:
- fit_score: 0-100, based on genuine skill/seniority match, not keyword overlap
- best_resume: "Security" or "SWE / Infra"
- reason: one short sentence

Watch for false-positive titles (e.g. "SoC" = System-on-Chip hardware, not Security
Operations Center -- score those near 0).

Return ONLY a JSON array, one object per job, same order, no other text:
[{{"fit_score": 85, "best_resume": "Security", "reason": "..."}}, ...]"""

    resp = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    text = resp.content[0].text.strip()
    text = re.sub(r"^```json\s*|\s*```$", "", text)
    return json.loads(text)


def main():
    sheet = get_sheet()
    tracked = already_tracked(sheet)

    candidates = collect_candidates()
    new_jobs = [c for c in candidates if c["url"] not in tracked]
    print(f"{len(candidates)} candidates found, {len(new_jobs)} not yet in sheet")

    if not new_jobs:
        print("Nothing new today.")
        return

    client = None
    if USE_AI_SCORING:
        import anthropic  # only imported/needed when a key is actually set
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        print("Scoring mode: AI (Claude) -- uses API credits")
    else:
        print("Scoring mode: free keyword matching -- $0 cost")

    today = date.today().isoformat()
    rows = []

    for i in range(0, len(new_jobs), BATCH_SIZE):
        batch = new_jobs[i:i + BATCH_SIZE]
        if USE_AI_SCORING:
            try:
                scores = score_batch(client, batch)
            except Exception as e:
                print(f"Scoring batch failed, skipping: {e}")
                continue
        else:
            scores = [keyword_score(j) for j in batch]
        for job, score in zip(batch, scores):
            if score.get("fit_score", 0) < FIT_THRESHOLD:
                continue
            rows.append([
                today, job["company"], job["title"], job["location"], "",
                job["source"], score["fit_score"], score["best_resume"],
                job["url"], "", "New", "", score.get("reason", ""),
            ])

    if rows:
        sheet.append_rows(rows, value_input_option="USER_ENTERED")
        print(f"Added {len(rows)} new rows (threshold: {FIT_THRESHOLD}+)")
    else:
        print("Nothing scored above threshold today.")


if __name__ == "__main__":
    main()
