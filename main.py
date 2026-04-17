"""
Scrape US job postings from the last 24h across multiple boards for a fixed
set of roles, dedupe by company, and append the raw results to a Google Sheet.

Enrichment + filtering (Perplexity employee/industry lookup, staffing/size cuts)
is not wired in yet — this run delivers the raw scrape for verification.
"""

import json
import os
from datetime import datetime, timezone

import pandas as pd
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

SITES = ["linkedin", "indeed", "glassdoor", "zip_recruiter", "google"]

RESULTS_PER_ROLE = 50
HOURS_OLD = 24

SHEET_COLUMNS = [
    "run_timestamp_utc",
    "job_title",
    "job_url",
    "company",
    "date_posted",
    "company_url",
    "location",
    "source",
]


def scrape_all() -> pd.DataFrame:
    frames = []
    for role in ROLES:
        print(f"Scraping: {role}")
        try:
            df = scrape_jobs(
                site_name=SITES,
                search_term=role,
                google_search_term=f"{role} jobs in the USA since yesterday",
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


def to_sheet_rows(df: pd.DataFrame) -> list[list[str]]:
    run_ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    out = pd.DataFrame(
        {
            "run_timestamp_utc": run_ts,
            "job_title": df.get("title", ""),
            "job_url": df.get("job_url", ""),
            "company": df.get("company", ""),
            "date_posted": df.get("date_posted", "").astype(str),
            "company_url": df.get("company_url", ""),
            "location": df.get("location", ""),
            "source": df.get("site", ""),
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

    # Write a header row if the sheet is empty.
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

    rows = to_sheet_rows(df)
    append_to_sheet(rows)
    print(f"Appended {len(rows)} rows to Google Sheet")


if __name__ == "__main__":
    main()
