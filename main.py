"""
Scrape US job postings from the last 24h across multiple boards for a fixed
set of roles, dedupe by company, enrich each company via Perplexity for
employee count + industry, drop staffing/nonprofit/educational/job-platform
companies and anything over 50 employees (or unknown size), then append the
survivors to a Google Sheet.
"""

import json
import os
import re
import time
from datetime import datetime, timezone

import pandas as pd
import requests
from google.oauth2.service_account import Credentials
import gspread
from jobspy import scrape_jobs


ROLES = [
    "data engineer",
    "analytics engineer",
    "data analyst",
    "AI engineer",
    "AI consultant",
    "data consultant",
]

SITES = ["linkedin", "indeed"]

RESULTS_PER_ROLE = 50
HOURS_OLD = 24
MAX_EMPLOYEES = 50

PERPLEXITY_URL = "https://api.perplexity.ai/chat/completions"
PERPLEXITY_MODEL = "sonar"
PERPLEXITY_TIMEOUT = 60
# Default tier rate limit is 50 RPM on sonar; stay just under.
PERPLEXITY_REQUEST_SPACING = 1.3
PERPLEXITY_MAX_RETRIES = 4
PERPLEXITY_INITIAL_BACKOFF = 10

SHEET_COLUMNS = [
    "run_timestamp_utc",
    "job_title",
    "job_url",
    "company",
    "company_url",
    "location",
    "source",
    "employee_count",
    "industry",
]


def scrape_all() -> pd.DataFrame:
    frames = []
    for role in ROLES:
        for site in SITES:
            print(f"Scraping: {role} @ {site}")
            try:
                df = scrape_jobs(
                    site_name=[site],
                    search_term=role,
                    location="United States",
                    results_wanted=RESULTS_PER_ROLE,
                    hours_old=HOURS_OLD,
                    country_indeed="USA",
                    linkedin_fetch_description=False,
                    verbose=1,
                )
            except Exception as e:
                print(f"  failed: {e}")
                continue
            if df is None or df.empty:
                print("  no results")
                continue
            df["search_role"] = role
            frames.append(df)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def dedupe_by_company(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["company"] = df["company"].fillna("").astype(str).str.strip()
    df = df[df["company"] != ""]
    df["_company_key"] = df["company"].str.lower()
    df = df.drop_duplicates(subset="_company_key", keep="first")
    return df.drop(columns=["_company_key"])


ENRICH_PROMPT = """Look up the US-based company "{company}" and return ONLY a JSON object (no prose, no markdown fences) with these exact keys:
- employee_count: current total number of employees as an integer, or null if you cannot find a reliable figure
- industry: short phrase (<= 12 words) describing what the company does
- is_staffing: true if this is a staffing agency, recruiting firm, RPO, or headhunter
- is_nonprofit: true if this is a nonprofit, NGO, foundation, or charity
- is_educational: true if this is a K-12 school, university, college, or other educational institution
- is_job_platform: true if this is a job board, job aggregator, or job-posting platform

Example output: {{"employee_count": 42, "industry": "B2B SaaS for accounting firms", "is_staffing": false, "is_nonprofit": false, "is_educational": false, "is_job_platform": false}}

If the company cannot be identified confidently, return employee_count as null."""


def _parse_enrichment_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.DOTALL)
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    return json.loads(text)


def enrich_company(company: str, api_key: str) -> dict | None:
    payload = {
        "model": PERPLEXITY_MODEL,
        "messages": [
            {"role": "user", "content": ENRICH_PROMPT.format(company=company)}
        ],
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    backoff = PERPLEXITY_INITIAL_BACKOFF
    for attempt in range(PERPLEXITY_MAX_RETRIES + 1):
        try:
            r = requests.post(
                PERPLEXITY_URL,
                json=payload,
                headers=headers,
                timeout=PERPLEXITY_TIMEOUT,
            )
        except Exception as e:
            print(f"  enrich failed for {company!r}: {e}")
            return None

        if r.status_code == 429:
            if attempt < PERPLEXITY_MAX_RETRIES:
                print(f"  429 for {company!r}, backing off {backoff}s (retry {attempt + 1}/{PERPLEXITY_MAX_RETRIES})")
                time.sleep(backoff)
                backoff *= 2
                continue
            print(f"  enrich failed for {company!r}: 429 after {PERPLEXITY_MAX_RETRIES} retries")
            return None

        try:
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
            return _parse_enrichment_json(content)
        except Exception as e:
            print(f"  enrich failed for {company!r}: {e}")
            return None

    return None


def enrich_all(df: pd.DataFrame) -> pd.DataFrame:
    api_key = os.environ["PERPLEXITY_API_KEY"]
    total = len(df)
    enrichments = []
    for i, company in enumerate(df["company"], 1):
        print(f"Enriching {i}/{total}: {company}")
        enrichments.append(enrich_company(company, api_key))
        if i < total:
            time.sleep(PERPLEXITY_REQUEST_SPACING)
    df = df.copy()
    df["_enrichment"] = enrichments
    return df


def _keep_row(e: dict | None) -> bool:
    if not e:
        return False
    count = e.get("employee_count")
    if count is None or not isinstance(count, int):
        return False
    if count > MAX_EMPLOYEES:
        return False
    if (
        e.get("is_staffing")
        or e.get("is_nonprofit")
        or e.get("is_educational")
        or e.get("is_job_platform")
    ):
        return False
    return True


def filter_and_flatten(df: pd.DataFrame) -> pd.DataFrame:
    df = df[df["_enrichment"].apply(_keep_row)].copy()
    df["employee_count"] = df["_enrichment"].apply(lambda e: e["employee_count"])
    df["industry"] = df["_enrichment"].apply(lambda e: e.get("industry", ""))
    return df.drop(columns=["_enrichment"])


def to_sheet_rows(df: pd.DataFrame) -> list[list[str]]:
    run_ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    out = pd.DataFrame(
        {
            "run_timestamp_utc": run_ts,
            "job_title": df.get("title", ""),
            "job_url": df.get("job_url", ""),
            "company": df.get("company", ""),
            "company_url": df.get("company_url", ""),
            "location": df.get("location", ""),
            "source": df.get("site", ""),
            "employee_count": df.get("employee_count", ""),
            "industry": df.get("industry", ""),
        }
    )
    out = out[SHEET_COLUMNS].fillna("").astype(str)
    return out.values.tolist()


def append_to_sheet(rows: list[list[str]]) -> None:
    creds_json = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    sheet_id = os.environ["GOOGLE_SHEET_ID"]
    worksheet_name = os.environ.get("GOOGLE_WORKSHEET_NAME", "Sheet1")

    creds = Credentials.from_service_account_info(
        json.loads(creds_json),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(sheet_id)
    ws = sh.worksheet(worksheet_name)

    if not ws.get_all_values():
        ws.append_row(SHEET_COLUMNS, value_input_option="USER_ENTERED")

    ws.append_rows(rows, value_input_option="USER_ENTERED")


def main() -> None:
    df = scrape_all()
    if df.empty:
        print("No jobs scraped; nothing to upload.")
        return
    print(f"Scraped {len(df)} rows before dedupe")

    df = dedupe_by_company(df)
    print(f"{len(df)} rows after dedupe by company")

    df = enrich_all(df)
    df = filter_and_flatten(df)
    print(f"{len(df)} rows after enrichment filter")
    if df.empty:
        print("Nothing left after filtering; nothing to upload.")
        return

    rows = to_sheet_rows(df)
    append_to_sheet(rows)
    print(f"Appended {len(rows)} rows to Google Sheet")


if __name__ == "__main__":
    main()
