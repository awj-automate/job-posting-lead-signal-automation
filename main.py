"""
Scrape US job postings from the last 24h across multiple boards for a fixed
set of roles, dedupe by company, enrich each company via Claude Haiku 4.5
(with server-side web search) for employee count + industry, drop
staffing/nonprofit/educational/job-platform companies and anything over 50
employees (or unknown size), then append the survivors to a Google Sheet.

A second "raw" sheet caches every company's lookup result across runs so
repeat companies skip the Claude call.
"""

import json
import os
from datetime import datetime, timezone

import anthropic
import pandas as pd
from jobspy import scrape_jobs

from sheets import SheetClient, fuzzy_find, normalize_company


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
WEB_SEARCH_TOOL_VERSION = "web_search_20250305"


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
    """Fuzzy-dedupe within a run so near-duplicates ("Acme Inc" vs "Acme")
    don't both flow through to lookup."""
    df = df.copy()
    df["company"] = df["company"].fillna("").astype(str).str.strip()
    df = df[df["company"] != ""]
    keeps: list = []
    seen: list[str] = []
    for idx, company in df["company"].items():
        key = normalize_company(company)
        if not key:
            continue
        if fuzzy_find(key, seen) is not None:
            continue
        seen.append(key)
        keeps.append(idx)
    return df.loc[keeps].copy()


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


def _keep_enrichment(e: dict | None) -> bool:
    if not e:
        return False
    count = e.get("employee_count")
    if count is None or not isinstance(count, int) or count > MAX_EMPLOYEES:
        return False
    if (
        e.get("is_staffing")
        or e.get("is_nonprofit")
        or e.get("is_educational")
        or e.get("is_job_platform")
    ):
        return False
    return True


def _s(v) -> str:
    """Safe string cast: None/NaN -> ""."""
    if v is None:
        return ""
    try:
        if pd.isna(v):
            return ""
    except (TypeError, ValueError):
        pass
    return str(v)


def _job_row(
    run_ts: str, job: pd.Series, employee_count: str, industry: str
) -> list[str]:
    """Shape a scraped job row into the SHEET_COLUMNS tuple."""
    return [
        run_ts,
        _s(job.get("title")),
        _s(job.get("job_url")),
        _s(job.get("company")),
        _s(job.get("company_url")),
        _s(job.get("location")),
        _s(job.get("site")),
        _s(employee_count),
        _s(industry),
    ]


def main() -> None:
    df = scrape_all()
    if df.empty:
        print("No jobs scraped; nothing to upload.")
        return
    print(f"Scraped {len(df)} rows before dedupe")

    df = dedupe_by_company(df)
    print(f"{len(df)} companies after within-run dedupe")
    if df.empty:
        return

    sheets = SheetClient()
    output_keys = sheets.load_output_company_keys()
    raw_cache = sheets.load_raw_cache()
    print(
        f"Cache: {len(raw_cache)} raw rows, "
        f"{len(output_keys)} companies already in output"
    )

    run_ts = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # Classify each scraped company.
    # - cached_pass: already Yes in raw cache, include in output
    # - needs_lookup: (job, existing_row_index_or_None) — run Claude, then update raw
    # - new_raw_rows: blank raw rows to append for never-seen companies
    cached_pass: list[tuple[pd.Series, str, str]] = []
    needs_lookup: list[tuple[pd.Series, int | None]] = []
    new_raw_rows: list[list[str]] = []

    raw_keys = list(raw_cache.keys())
    for _, job in df.iterrows():
        key = normalize_company(_s(job.get("company")))
        if not key:
            continue
        match_key = fuzzy_find(key, raw_keys)
        if match_key is not None:
            cached = raw_cache[match_key]
            passed = cached.passed_lookup.strip().lower()
            if passed == "yes":
                cached_pass.append((job, cached.employee_count, cached.industry))
            elif passed == "no":
                pass  # excluded by prior lookup
            else:
                # Row exists but lookup never completed — retry, reuse row.
                needs_lookup.append((job, cached.row_index))
        else:
            needs_lookup.append((job, None))
            new_raw_rows.append(_job_row(run_ts, job, "", "") + [""])

    print(
        f"Classification: {len(cached_pass)} cached-pass, "
        f"{len(needs_lookup)} need lookup "
        f"({len(new_raw_rows)} new to raw sheet)"
    )

    # Append new raw rows and bind their row indices to needs_lookup entries.
    if new_raw_rows:
        new_row_indices = sheets.append_raw_rows(new_raw_rows)
        it = iter(new_row_indices)
        needs_lookup = [
            (job, ri if ri is not None else next(it))
            for job, ri in needs_lookup
        ]

    # Enrich — the only step that calls Claude.
    client = anthropic.Anthropic(max_retries=CLAUDE_MAX_RETRIES)
    lookup_results: list[tuple[pd.Series, int, dict | None]] = []
    for i, (job, row_idx) in enumerate(needs_lookup, 1):
        company = _s(job.get("company"))
        print(f"Enriching {i}/{len(needs_lookup)}: {company}")
        lookup_results.append((job, row_idx, enrich_company(client, company)))

    # Write enrichment results back to the raw sheet. Failed lookups (None)
    # stay blank so the next run retries them.
    raw_updates = []
    for job, row_idx, e in lookup_results:
        if e is None:
            continue
        passed = "Yes" if _keep_enrichment(e) else "No"
        emp = e.get("employee_count")
        emp_str = str(emp) if isinstance(emp, int) else ""
        raw_updates.append((row_idx, passed, emp_str, _s(e.get("industry"))))
    sheets.update_raw_lookups(raw_updates)

    # Build output rows: cached passes + newly-passed lookups,
    # skipping any company already present in the output sheet.
    output_rows: list[list[str]] = []
    for job, emp, ind in cached_pass:
        key = normalize_company(_s(job.get("company")))
        if fuzzy_find(key, output_keys) is not None:
            continue
        output_rows.append(_job_row(run_ts, job, emp, ind))
        output_keys.add(key)

    for job, _row_idx, e in lookup_results:
        if not _keep_enrichment(e):
            continue
        key = normalize_company(_s(job.get("company")))
        if fuzzy_find(key, output_keys) is not None:
            continue
        output_rows.append(
            _job_row(run_ts, job, str(e["employee_count"]), _s(e.get("industry")))
        )
        output_keys.add(key)

    if not output_rows:
        print("Nothing new to append to output sheet.")
        return
    sheets.append_output_rows(output_rows)
    print(f"Appended {len(output_rows)} rows to output sheet")


if __name__ == "__main__":
    main()
