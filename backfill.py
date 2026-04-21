"""
One-shot backfill from Google Sheets to Neon.

Runs automatically on startup when the companies table is empty and both
Sheets env vars are still set. After the first successful run it's a no-op.
"""

import json
import os
import re
from datetime import datetime

import psycopg

from matcher import normalize_company


def _sheets_available() -> bool:
    return all(
        os.environ.get(v)
        for v in ("GOOGLE_SERVICE_ACCOUNT_JSON", "GOOGLE_SHEET_ID", "RAW_SHEET_ID")
    )


def _open_ws(sheet_id_env: str, ws_name_env: str):
    import gspread
    from google.oauth2.service_account import Credentials

    creds = Credentials.from_service_account_info(
        json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]),
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
    )
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(os.environ[sheet_id_env])
    return sh.worksheet(os.environ.get(ws_name_env, "Sheet1"))


def _int_or_none(s: str) -> int | None:
    if not s:
        return None
    try:
        return int(s.strip())
    except ValueError:
        return None


def _parse_ts(s: str | None):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _derive_domain(company_url: str) -> str | None:
    if not company_url:
        return None
    m = re.search(r"https?://([^/?#]+)", company_url)
    if not m:
        return None
    host = m.group(1).lower()
    return host[4:] if host.startswith("www.") else host


def _rows_from_sheet(ws) -> list[dict]:
    values = ws.get_all_values()
    if len(values) < 2:
        return []
    idx = {c: i for i, c in enumerate(values[0])}

    def cell(row, col):
        j = idx.get(col)
        return row[j].strip() if j is not None and j < len(row) else ""

    return [
        {
            "company": cell(row, "company"),
            "run_ts": cell(row, "run_timestamp_utc"),
            "job_title": cell(row, "job_title"),
            "job_url": cell(row, "job_url"),
            "company_url": cell(row, "company_url"),
            "location": cell(row, "location"),
            "source": cell(row, "source"),
            "employee_count": _int_or_none(cell(row, "employee_count")),
            "industry": cell(row, "industry") or None,
            "passed_lookup": (cell(row, "passed_lookup") or "").strip().lower() or None,
        }
        for row in values[1:]
    ]


def _merge_by_key(rows: list[dict]) -> dict[str, dict]:
    """Collapse rows by normalized company key. Precedence:
    passed_lookup: 'yes' > 'no' > None; fields: first non-empty wins;
    run_ts: earliest."""
    merged: dict[str, dict] = {}
    for row in rows:
        key = normalize_company(row["company"])
        if not key:
            continue
        prior = merged.get(key)
        if prior is None:
            merged[key] = dict(row, _key=key)
            continue
        if row["passed_lookup"] == "yes" or (
            row["passed_lookup"] == "no" and prior["passed_lookup"] is None
        ):
            prior["passed_lookup"] = row["passed_lookup"]
        for f in ("employee_count", "industry", "company_url"):
            if not prior.get(f) and row.get(f):
                prior[f] = row[f]
        if row["run_ts"] and (
            not prior["run_ts"] or row["run_ts"] < prior["run_ts"]
        ):
            prior["run_ts"] = row["run_ts"]
    return merged


def run_backfill(conn: psycopg.Connection) -> None:
    if not _sheets_available():
        print("Backfill: Sheets env vars not set; skipping.")
        return

    print("Backfill: reading sheets...")
    raw_rows = _rows_from_sheet(_open_ws("RAW_SHEET_ID", "RAW_WORKSHEET_NAME"))
    out_rows = _rows_from_sheet(_open_ws("GOOGLE_SHEET_ID", "GOOGLE_WORKSHEET_NAME"))
    for r in out_rows:
        r["passed_lookup"] = "yes"  # output sheet only ever held passers
    print(f"Backfill: {len(raw_rows)} raw rows, {len(out_rows)} output rows")

    merged = _merge_by_key(raw_rows + out_rows)
    print(f"Backfill: {len(merged)} unique companies after merge")

    # Insert companies. Capture id per normalized key.
    key_to_id: dict[str, int] = {}
    with conn.cursor() as cur:
        for key, r in merged.items():
            cur.execute(
                """
                INSERT INTO companies (
                    name, normalized_key, domain, employee_count, industry,
                    passed_lookup, first_seen_at, last_looked_up_at
                )
                VALUES (%s, %s, %s, %s, %s, %s,
                        COALESCE(%s, NOW()), %s)
                ON CONFLICT (normalized_key) DO NOTHING
                RETURNING id
                """,
                (
                    r["company"],
                    key,
                    _derive_domain(r.get("company_url", "")),
                    r["employee_count"],
                    r["industry"],
                    r["passed_lookup"],
                    _parse_ts(r["run_ts"]),
                    _parse_ts(r["run_ts"]) if r["passed_lookup"] else None,
                ),
            )
            row = cur.fetchone()
            if row:
                key_to_id[key] = row[0]
            else:
                cur.execute(
                    "SELECT id FROM companies WHERE normalized_key = %s", (key,)
                )
                key_to_id[key] = cur.fetchone()[0]

    # Insert jobs — dedup by (normalized_key, title, url) across both sheets.
    seen: set[tuple] = set()
    job_tuples = []
    for row in raw_rows + out_rows:
        key = normalize_company(row["company"])
        cid = key_to_id.get(key)
        if cid is None:
            continue
        sig = (key, row["job_title"], row["job_url"])
        if sig in seen:
            continue
        seen.add(sig)
        job_tuples.append(
            (
                cid,
                row["job_title"] or None,
                row["job_url"] or None,
                row["location"] or None,
                row["source"] or None,
                None,  # search_role not stored in sheets
                _parse_ts(row["run_ts"]),
            )
        )
    print(f"Backfill: inserting {len(job_tuples)} job rows...")
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO jobs
                (company_id, title, url, location, source, search_role, run_timestamp)
            VALUES (%s, %s, %s, %s, %s, %s, COALESCE(%s, NOW()))
            """,
            job_tuples,
        )
    print("Backfill: done.")
