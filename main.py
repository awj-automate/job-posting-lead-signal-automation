"""
Scrape US job postings from the last 24h across multiple boards for a fixed
set of roles, dedupe by company, enrich each company via Claude Haiku 4.5
(with server-side web search) for employee count + industry, drop
staffing/nonprofit/educational/job-platform companies and anything over 50
employees (or unknown size), then append the survivors to a Google Sheet.
"""

import json
import os
from datetime import datetime, timezone

import anthropic
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

SITES = ["linkedin", "indeed"]

RESULTS_PER_ROLE = 50
HOURS_OLD = 24
MAX_EMPLOYEES = 50

CLAUDE_MODEL = "claude-haiku-4-5"
CLAUDE_MAX_TOKENS = 1024
CLAUDE_MAX_RETRIES = 4
# Web search tool version — the older tool ID is widely compatible with Haiku.
WEB_SEARCH_TOOL_VERSION = "web_search_20250305"

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


ENRICH_SCHEMA = {
    "type": "object",
    "properties": {
        "employee_count": {
            "type": ["integer", "null"],
            "description": "Current total number of employees. Null if not confidently known.",
        },
        "industry": {
            "type": "string",
            "description": "Short phrase (<= 12 words) describing what the company does.",
        },
        "is_staffing": {
            "type": "boolean",
            "description": "True if staffing agency, recruiting firm, RPO, or headhunter.",
        },
        "is_nonprofit": {
            "type": "boolean",
            "description": "True if nonprofit, NGO, foundation, or charity.",
        },
        "is_educational": {
            "type": "boolean",
            "description": "True if K-12 school, university, college, or educational institution.",
        },
        "is_job_platform": {
            "type": "boolean",
            "description": "True if job board, job aggregator, or job-posting platform.",
        },
    },
    "required": [
        "employee_count",
        "industry",
        "is_staffing",
        "is_nonprofit",
        "is_educational",
        "is_job_platform",
    ],
    "additionalProperties": False,
}

ENRICH_PROMPT = """Look up the US-based company "{company}" using web search and return the structured fields described in the output schema.

For employee_count: if you cannot find a reliable headcount from a credible source, return null rather than guessing.
For industry: a short phrase (<= 12 words) describing what the company does.
Set the boolean flags accurately — only true if clearly applicable."""


def enrich_company(client: anthropic.Anthropic, company: str) -> dict | None:
    messages = [
        {"role": "user", "content": ENRICH_PROMPT.format(company=company)}
    ]

    # One continuation in case the server-side tool loop hits pause_turn.
    for _ in range(2):
        try:
            response = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=CLAUDE_MAX_TOKENS,
                messages=messages,
                tools=[{"type": WEB_SEARCH_TOOL_VERSION, "name": "web_search"}],
                output_config={
                    "format": {"type": "json_schema", "schema": ENRICH_SCHEMA}
                },
            )
        except Exception as e:
            print(f"  enrich failed for {company!r}: {e}")
            return None

        if response.stop_reason == "pause_turn":
            messages = [
                messages[0],
                {"role": "assistant", "content": response.content},
            ]
            continue

        if response.stop_reason == "refusal":
            print(f"  enrich refused for {company!r}")
            return None

        text = next(
            (b.text for b in response.content if b.type == "text"), None
        )
        if not text:
            print(f"  enrich failed for {company!r}: no text block in response")
            return None

        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            print(f"  enrich failed for {company!r}: {e}")
            return None

    print(f"  enrich failed for {company!r}: pause_turn not resolved")
    return None


def enrich_all(df: pd.DataFrame) -> pd.DataFrame:
    client = anthropic.Anthropic(max_retries=CLAUDE_MAX_RETRIES)
    total = len(df)
    enrichments = []
    for i, company in enumerate(df["company"], 1):
        print(f"Enriching {i}/{total}: {company}")
        enrichments.append(enrich_company(client, company))
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
