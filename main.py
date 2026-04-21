"""
Scrape US job postings from the last 24h across multiple boards for a fixed
set of roles, dedupe by company, enrich each company via Claude Haiku 4.5
(with server-side web search) for employee count + industry, drop
staffing/nonprofit/educational/job-platform companies and anything over 50
employees (or unknown size), then persist survivors + historical job data to
Neon Postgres.

Each invocation writes a row to the `runs` table wrapping the whole thing,
so failures are auditable.
"""

import json
import re
import time
import traceback

import anthropic
import pandas as pd
from jobspy import scrape_jobs

import contacts
import db
import instantly
from matcher import fuzzy_find, normalize_company


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

# Cap per-run contact discovery to keep runs bounded.
MAX_CONTACT_DISCOVERIES_PER_RUN = 50
CONTACT_DISCOVERY_SLEEP_SECONDS = 0.5

# When True, the pipeline stops after the classification step: no company
# upserts, no Claude enrichment, no contact discovery, no Instantly sync.
# Just scrape + dedupe + look everything up against the DB cache and log
# how many rows each bucket would have produced.
DRY_RUN = False


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
    """Fuzzy-dedupe within a run so near-duplicates don't both reach lookup."""
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
        "domain": {
            "type": ["string", "null"],
            "description": "Primary company website domain, e.g. 'example.com'.",
        },
        "is_staffing": {"type": "boolean"},
        "is_nonprofit": {"type": "boolean"},
        "is_educational": {"type": "boolean"},
        "is_job_platform": {"type": "boolean"},
    },
    "required": [
        "employee_count",
        "industry",
        "domain",
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
For domain: the company's primary website domain (e.g., "example.com") without protocol or "www.". Do NOT return a job board domain like linkedin.com or indeed.com. Return null if uncertain.
Set the boolean flags accurately — only true if clearly applicable."""


def enrich_company(client: anthropic.Anthropic, company: str) -> dict | None:
    messages = [{"role": "user", "content": ENRICH_PROMPT.format(company=company)}]

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


def _classify_enrichment(e: dict | None) -> tuple[str | None, str | None]:
    """(passed_lookup, rejection_reason). passed_lookup=None means lookup
    itself failed and should be retried next run."""
    if not e:
        return None, None
    count = e.get("employee_count")
    if count is None or not isinstance(count, int):
        return "no", "size_unknown"
    if count > MAX_EMPLOYEES:
        return "no", "too_large"
    for flag, reason in (
        ("is_staffing", "staffing"),
        ("is_nonprofit", "nonprofit"),
        ("is_educational", "educational"),
        ("is_job_platform", "job_platform"),
    ):
        if e.get(flag):
            return "no", reason
    return "yes", None


def _s(v) -> str:
    if v is None:
        return ""
    try:
        if pd.isna(v):
            return ""
    except (TypeError, ValueError):
        pass
    return str(v)


def _domain_from_url(url: str) -> str | None:
    if not url:
        return None
    m = re.search(r"https?://([^/?#]+)", url)
    if not m:
        return None
    host = m.group(1).lower()
    return host[4:] if host.startswith("www.") else host


def main() -> None:
    conn = db.connect()
    db.init_schema(conn)

    if DRY_RUN:
        print("*** DRY RUN — no DB writes, no API calls past the scrape ***")
        run_id = None
    else:
        run_id = db.start_run(conn)
        print(f"Starting run {run_id}")
    try:
        df = scrape_all()
        if df.empty:
            print("No jobs scraped.")
            if not DRY_RUN:
                db.finish_run(conn, run_id, jobs_scraped=0)
            return
        scraped_count = len(df)
        print(f"Scraped {scraped_count} rows before dedupe")

        df = dedupe_by_company(df)
        print(f"{len(df)} companies after within-run dedupe")
        if df.empty:
            if not DRY_RUN:
                db.finish_run(conn, run_id, jobs_scraped=0)
            return

        cache = db.load_companies(conn)
        cache_keys = list(cache.keys())
        print(f"Loaded {len(cache)} companies from DB")

        # (company_id, job_row, needs_lookup) — needs_lookup skipped when
        # the company is already cached yes or no.
        entries: list[tuple[int, pd.Series, bool]] = []
        companies_new = 0
        companies_cached_yes = 0
        companies_cached_no = 0
        companies_pending_lookup = 0

        for _, job in df.iterrows():
            name = _s(job.get("company"))
            key = normalize_company(name)
            if not key:
                continue
            match = fuzzy_find(key, cache_keys)
            if match is not None:
                c = cache[match]
                passed = (c.passed_lookup or "").lower()
                if passed == "yes":
                    companies_cached_yes += 1
                    if not DRY_RUN:
                        entries.append((c.id, job, False))
                elif passed == "no":
                    companies_cached_no += 1
                    if not DRY_RUN:
                        entries.append((c.id, job, False))
                else:
                    companies_pending_lookup += 1
                    if not DRY_RUN:
                        entries.append((c.id, job, True))
            else:
                companies_new += 1
                if not DRY_RUN:
                    cid = db.upsert_company(
                        conn, name, key, _domain_from_url(_s(job.get("company_url")))
                    )
                    cache[key] = db.Company(
                        id=cid,
                        name=name,
                        normalized_key=key,
                        domain=_domain_from_url(_s(job.get("company_url"))),
                        employee_count=None,
                        industry=None,
                        passed_lookup=None,
                        rejection_reason=None,
                    )
                    cache_keys.append(key)
                    entries.append((cid, job, True))

        would_enrich = companies_new + companies_pending_lookup
        print(
            "\n=== Classification summary ===\n"
            f"  Scraped (pre-dedupe):                     {scraped_count}\n"
            f"  After within-run dedupe:                  {len(df)}\n"
            f"  Already cached Yes (would skip enrich):   {companies_cached_yes}\n"
            f"  Already cached No  (would skip enrich):   {companies_cached_no}\n"
            f"  Known to DB but lookup pending (retry):   {companies_pending_lookup}\n"
            f"  Never seen before (would insert+enrich):  {companies_new}\n"
            f"  => WOULD ENRICH NEXT:                     {would_enrich} companies\n"
        )

        if DRY_RUN:
            print("Dry run complete — exiting before any writes.")
            return

        # Record every scraped job (regardless of company status) for history.
        db.insert_jobs(
            conn,
            [
                (
                    cid,
                    _s(job.get("title")) or None,
                    _s(job.get("job_url")) or None,
                    _s(job.get("location")) or None,
                    _s(job.get("site")) or None,
                    _s(job.get("search_role")) or None,
                )
                for cid, job, _ in entries
            ],
        )

        # Enrich distinct pending companies.
        pending: list[tuple[int, str]] = []
        seen_ids: set[int] = set()
        for cid, job, needs in entries:
            if not needs or cid in seen_ids:
                continue
            seen_ids.add(cid)
            pending.append((cid, _s(job.get("company"))))

        print(f"Enriching {len(pending)} companies")
        client = anthropic.Anthropic(max_retries=CLAUDE_MAX_RETRIES)
        for i, (cid, name) in enumerate(pending, 1):
            print(f"Enriching {i}/{len(pending)}: {name}")
            e = enrich_company(client, name)
            passed, reason = _classify_enrichment(e)
            if passed is None:
                continue  # lookup failed — leave null, retry next run
            db.update_company_lookup(
                conn,
                cid,
                passed,
                e.get("employee_count") if isinstance(e.get("employee_count"), int) else None,
                e.get("industry") or None,
                reason,
                domain=e.get("domain") or None,
            )

        # Phase 2: find + verify contacts for passing companies that haven't
        # been through contact discovery yet.
        contacts_found = 0
        pending_contacts = db.load_companies_pending_contacts(
            conn, MAX_CONTACT_DISCOVERIES_PER_RUN
        )
        print(f"Contact discovery: {len(pending_contacts)} companies to process")
        for i, c in enumerate(pending_contacts, 1):
            print(f"[{i}/{len(pending_contacts)}] {c.name}")
            contacts_found += contacts.discover_for_company(
                conn, c.id, c.name, c.domain, client,
            )
            if i < len(pending_contacts):
                time.sleep(CONTACT_DISCOVERY_SLEEP_SECONDS)

        # Phase 3: push verified unsynced contacts to Instantly.
        contacts_synced = instantly.sync_unsynced(conn)

        db.finish_run(
            conn,
            run_id,
            jobs_scraped=scraped_count,
            companies_new=companies_new,
            companies_cached_yes=companies_cached_yes,
            companies_cached_no=companies_cached_no,
            contacts_found=contacts_found,
            contacts_verified_ok=contacts_found,
            contacts_synced_to_instantly=contacts_synced,
        )
        print(f"Run {run_id} complete")
    except Exception:
        if run_id is not None:
            db.record_run_error(conn, run_id, traceback.format_exc())
        raise


if __name__ == "__main__":
    main()
