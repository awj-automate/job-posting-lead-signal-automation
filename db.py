"""Neon/Postgres persistence layer.

Thin wrapper around psycopg3. Uses autocommit so each call is its own
transaction — the pipeline is a one-shot cron job, not a long-running server.
"""

import os
import re
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg.rows import dict_row


SCHEMA_PATH = Path(__file__).with_name("schema.sql")


@dataclass
class Company:
    id: int
    name: str
    normalized_key: str
    domain: str | None
    employee_count: int | None
    industry: str | None
    passed_lookup: str | None
    rejection_reason: str | None


@dataclass
class PendingContactCompany:
    id: int
    name: str
    domain: str | None


def connect() -> psycopg.Connection:
    return psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)


def init_schema(conn: psycopg.Connection) -> None:
    """Idempotent — all CREATE statements are IF NOT EXISTS.

    psycopg3's execute() is single-statement under the extended query
    protocol, so we strip line comments, split on `;`, and run each piece."""
    sql = re.sub(r"--[^\n]*", "", SCHEMA_PATH.read_text())
    statements = [s.strip() for s in sql.split(";") if s.strip()]
    with conn.cursor() as cur:
        for stmt in statements:
            cur.execute(stmt)


def companies_empty(conn: psycopg.Connection) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM companies LIMIT 1")
        return cur.fetchone() is None


def load_companies(conn: psycopg.Connection) -> dict[str, Company]:
    """Whole companies table keyed by normalized_key."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT id, name, normalized_key, domain, employee_count, "
            "industry, passed_lookup, rejection_reason FROM companies"
        )
        return {r["normalized_key"]: Company(**r) for r in cur.fetchall()}


def upsert_company(
    conn: psycopg.Connection,
    name: str,
    normalized_key: str,
    domain: str | None = None,
) -> int:
    """Insert a company (or return the existing id by normalized_key).

    Leaves passed_lookup NULL on insert so the enrichment step fills it in.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO companies (name, normalized_key, domain)
            VALUES (%s, %s, %s)
            ON CONFLICT (normalized_key) DO UPDATE
                SET name   = EXCLUDED.name,
                    domain = COALESCE(companies.domain, EXCLUDED.domain)
            RETURNING id
            """,
            (name, normalized_key, domain),
        )
        return cur.fetchone()[0]


def update_company_lookup(
    conn: psycopg.Connection,
    company_id: int,
    passed_lookup: str,  # 'yes' | 'no'
    employee_count: int | None,
    industry: str | None,
    rejection_reason: str | None = None,
    domain: str | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE companies SET
                passed_lookup     = %s,
                employee_count    = %s,
                industry          = %s,
                rejection_reason  = %s,
                domain            = COALESCE(domain, %s),
                last_looked_up_at = NOW()
            WHERE id = %s
            """,
            (
                passed_lookup,
                employee_count,
                industry,
                rejection_reason,
                domain,
                company_id,
            ),
        )


def load_companies_pending_contacts(
    conn: psycopg.Connection, limit: int
) -> list[PendingContactCompany]:
    """Passed companies whose contacts haven't been discovered yet."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT id, name, domain FROM companies
            WHERE passed_lookup = 'yes' AND contacts_discovered_at IS NULL
            ORDER BY first_seen_at
            LIMIT %s
            """,
            (limit,),
        )
        return [PendingContactCompany(**r) for r in cur.fetchall()]


def save_contact(
    conn: psycopg.Connection,
    company_id: int,
    first_name: str,
    last_name: str,
    title: str,
    email: str,
    email_status: str,
    email_source: str,
) -> None:
    """Insert a verified contact. Ignores duplicates on (company_id, email)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO contacts (
                company_id, first_name, last_name, title,
                email, email_status, email_source, verified_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (company_id, email) DO NOTHING
            """,
            (
                company_id, first_name, last_name, title,
                email, email_status, email_source,
            ),
        )


def mark_company_contacts_discovered(
    conn: psycopg.Connection, company_id: int
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE companies SET contacts_discovered_at = NOW() WHERE id = %s",
            (company_id,),
        )


def load_unsynced_contacts(conn: psycopg.Connection) -> list[dict]:
    """Contacts verified 'ok' that haven't been pushed to Instantly yet."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT c.id, c.email, c.first_name, c.last_name, c.title,
                   co.name AS company_name, co.domain AS company_domain
            FROM contacts c
            JOIN companies co ON co.id = c.company_id
            WHERE c.email_status = 'ok'
              AND c.email IS NOT NULL
              AND c.synced_to_instantly_at IS NULL
            ORDER BY c.id
            """
        )
        return cur.fetchall()


def mark_contact_synced(
    conn: psycopg.Connection, contact_id: int, instantly_lead_id: str
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE contacts SET
                synced_to_instantly_at = NOW(),
                instantly_lead_id = %s
            WHERE id = %s
            """,
            (instantly_lead_id, contact_id),
        )


def insert_jobs(
    conn: psycopg.Connection,
    rows: list[tuple[int, str, str, str, str, str]],
) -> None:
    """rows = [(company_id, title, url, location, source, search_role), ...]"""
    if not rows:
        return
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO jobs (company_id, title, url, location, source, search_role)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            rows,
        )


def start_run(conn: psycopg.Connection) -> int:
    with conn.cursor() as cur:
        cur.execute("INSERT INTO runs DEFAULT VALUES RETURNING id")
        return cur.fetchone()[0]


_RUN_COUNT_COLS = {
    "jobs_scraped",
    "companies_new",
    "companies_cached_yes",
    "companies_cached_no",
    "contacts_found",
    "contacts_verified_ok",
    "contacts_synced_to_instantly",
}


def finish_run(conn: psycopg.Connection, run_id: int, **counts) -> None:
    """Set finished_at plus any provided count columns."""
    fields = {k: v for k, v in counts.items() if k in _RUN_COUNT_COLS}
    sets = ["finished_at = NOW()"] + [f"{k} = %s" for k in fields]
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE runs SET {', '.join(sets)} WHERE id = %s",
            [*fields.values(), run_id],
        )


def record_run_error(conn: psycopg.Connection, run_id: int, error_text: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE runs SET finished_at = NOW(), error = %s WHERE id = %s",
            (error_text, run_id),
        )
