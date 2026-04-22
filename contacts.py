"""
Contact discovery via AnyMailFinder's decision-maker endpoint, with a
MillionVerifier check as the final gate.

One AMF call per passing company, tries CEO/founder first, falls through
to other C-suite categories. If AMF returns a `valid` email we run it
through MV; only `ok` results get saved. Marks the company as discovered
either way so the next run doesn't retry.
"""

import os

import requests

import db


AMF_ENDPOINT = "https://api.anymailfinder.com/v5.1/find-email/decision-maker"
AMF_HTTP_TIMEOUT = 200

MV_ENDPOINT = "https://api.millionverifier.com/api/v3/"
MV_TIMEOUT_SECONDS = 10
MV_HTTP_TIMEOUT = 20

# Domains that belong to job boards / generic platforms, not the employer.
JUNK_DOMAINS = {
    "linkedin.com", "indeed.com", "glassdoor.com", "ziprecruiter.com",
    "lever.co", "greenhouse.io", "workable.com", "ashbyhq.com",
    "wellfound.com", "angel.co", "builtin.com", "dice.com",
    "jobvite.com", "smartrecruiters.com", "breezy.hr", "bamboohr.com",
    "monster.com", "simplyhired.com",
}


def _is_valid_company_domain(domain: str | None) -> bool:
    if not domain or "." not in domain or " " in domain:
        return False
    d = domain.lower()
    for j in JUNK_DOMAINS:
        if d == j or d.endswith("." + j):
            return False
    return True


def _amf_decision_maker(
    domain: str | None, company_name: str
) -> dict | None:
    """Call AMF decision-maker. Returns contact dict or None.

    Tries categories in priority order (AMF picks the first that resolves
    to a valid email — we only pay 2 credits when one does)."""
    if not domain and not company_name:
        return None
    payload = {
        "decision_maker_category": [
            "ceo", "operations", "engineering", "finance", "sales", "marketing",
        ],
    }
    if _is_valid_company_domain(domain):
        payload["domain"] = domain
    else:
        payload["company_name"] = company_name

    try:
        resp = requests.post(
            AMF_ENDPOINT,
            headers={
                "Authorization": os.environ["ANYMAILFINDER_API_KEY"],
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=AMF_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        print(f"  AMF error for {company_name!r}: {e}")
        return None

    if data.get("email_status") != "valid" or not data.get("valid_email"):
        return None

    full_name = (data.get("person_full_name") or "").split()
    return {
        "email": data["valid_email"],
        "first_name": full_name[0] if full_name else "",
        "last_name": " ".join(full_name[1:]) if len(full_name) > 1 else "",
        "title": data.get("person_job_title") or "",
    }


def _verify_mv(email: str) -> str:
    """Returns the MV result string:
    ok / catch_all / unknown / invalid / disposable / error."""
    try:
        resp = requests.get(
            MV_ENDPOINT,
            params={
                "api": os.environ["MILLIONVERIFIER_API_KEY"],
                "email": email,
                "timeout": MV_TIMEOUT_SECONDS,
            },
            timeout=MV_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        return (resp.json().get("result") or "error").strip().lower() or "error"
    except requests.RequestException as e:
        print(f"  MV error for {email}: {e}")
        return "error"


def discover_for_company(
    conn,
    company_id: int,
    company_name: str,
    domain: str | None,
) -> int:
    """AMF lookup → MV verification → save only if MV returns 'ok'.
    Marks the company as discovered either way so it isn't retried."""
    print(f"AMF lookup: {company_name!r} (domain={domain})")
    saved = 0
    amf = _amf_decision_maker(domain, company_name)
    if amf:
        print(f"  AMF returned: {amf['email']} ({amf['title']})")
        mv_status = _verify_mv(amf["email"])
        if mv_status == "ok":
            print("  MV confirmed ok — saving")
            db.save_contact(
                conn, company_id,
                amf["first_name"], amf["last_name"], amf["title"],
                amf["email"], "ok", "amf",
            )
            saved = 1
        else:
            print(f"  MV rejected ({mv_status}) — discarding")
    else:
        print("  AMF returned no valid email")
    db.mark_company_contacts_discovered(conn, company_id)
    return saved
