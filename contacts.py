"""
Contact discovery for passing companies.

Per company:
1. Ask Claude Haiku 4.5 (with web search) for up to 3 C-level decision makers.
2. Generate common email patterns from name + domain.
3. Verify candidates with MillionVerifier; keep the first `ok` per person.
4. If the cheap path yields zero verified emails, fall back to AnyMailFinder's
   decision-maker endpoint (one call, returns at most one contact).

All verified contacts are written to the `contacts` table. The company's
`contacts_discovered_at` is stamped at the end of the pass so we only try
once per company.
"""

import json
import os
import re
import time
import unicodedata

import anthropic
import requests

import db


CLAUDE_MODEL = "claude-haiku-4-5"
CLAUDE_MAX_TOKENS = 1024
WEB_SEARCH_TOOL_VERSION = "web_search_20250305"
# Cap web searches per Claude call. Without this the model can fire 10+
# searches and quietly burn $0.10+ per company in search fees alone.
CONTACT_MAX_SEARCHES = 5

MV_ENDPOINT = "https://api.millionverifier.com/api/v3/"
MV_TIMEOUT_SECONDS = 10
MV_HTTP_TIMEOUT = 20
MV_SLEEP_BETWEEN_CALLS = 0.1

AMF_ENDPOINT = "https://api.anymailfinder.com/v5.1/find-email/decision-maker"
AMF_HTTP_TIMEOUT = 200

# Domains that belong to job boards / generic platforms, not the employer.
JUNK_DOMAINS = {
    "linkedin.com", "indeed.com", "glassdoor.com", "ziprecruiter.com",
    "lever.co", "greenhouse.io", "workable.com", "ashbyhq.com",
    "wellfound.com", "angel.co", "builtin.com", "dice.com",
    "jobvite.com", "smartrecruiters.com", "breezy.hr", "bamboohr.com",
    "monster.com", "simplyhired.com",
}


CONTACT_SCHEMA = {
    "type": "object",
    "properties": {
        "contacts": {
            "type": "array",
            "maxItems": 3,
            "items": {
                "type": "object",
                "properties": {
                    "first_name": {"type": "string"},
                    "last_name": {"type": "string"},
                    "title": {"type": "string"},
                },
                "required": ["first_name", "last_name", "title"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["contacts"],
    "additionalProperties": False,
}


CONTACT_PROMPT = """Identify the current C-level decision makers at the US-based company "{company}" (domain: {domain}). Use web search.

Return up to 3 individuals whose CURRENT (not former) titles are among:
- Chief Executive Officer (CEO)
- Chief Operating Officer (COO)
- Chief Technology Officer (CTO)
- Chief Financial Officer (CFO)
- Chief Revenue Officer (CRO)
- Chief Marketing Officer (CMO)
- Founder / Co-founder
- Owner

Prioritize CEO / Founder / Owner before other C-suite titles.

For each person, return their first_name, last_name, and exact current title as publicly listed. Only include people you can verify with confidence from credible sources — do not guess or fabricate.

If you cannot confidently identify any qualifying person, return an empty array."""


def _ascii_lower(s: str) -> str:
    """Strip accents + non-letters, lowercase."""
    if not s:
        return ""
    nfd = unicodedata.normalize("NFD", s)
    stripped = "".join(
        c for c in nfd if not unicodedata.combining(c) and c.isascii()
    )
    return re.sub(r"[^a-zA-Z]", "", stripped).lower()


def _is_valid_company_domain(domain: str | None) -> bool:
    if not domain or "." not in domain or " " in domain:
        return False
    d = domain.lower()
    for j in JUNK_DOMAINS:
        if d == j or d.endswith("." + j):
            return False
    return True


def generate_patterns(first: str, last: str, domain: str) -> list[str]:
    """Three email candidates in priority order — matches the email-finder
    project's default (non-deep) patterns. Uses the first word of `first`
    and the last word of `last` so middle names / multi-part surnames
    don't muddle the permutations."""
    fw = first.split()
    lw = last.split()
    f = _ascii_lower(fw[0]) if fw else ""
    l = _ascii_lower(lw[-1]) if lw else ""
    if not f or not l or not _is_valid_company_domain(domain):
        return []
    d = domain.lower()
    return [
        f"{f}@{d}",          # {firstname}
        f"{f[0]}{l}@{d}",    # {firstinitial}{lastname}
        f"{f}.{l}@{d}",      # {firstname}.{lastname}
    ]


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
        print(f"    MV error for {email}: {e}")
        return "error"


def _try_patterns(first: str, last: str, domain: str) -> str | None:
    """Try each pattern in priority order until one verifies as 'ok'."""
    for email in generate_patterns(first, last, domain):
        status = _verify_mv(email)
        if status == "ok":
            return email
        time.sleep(MV_SLEEP_BETWEEN_CALLS)
    return None


def _claude_find_people(
    client: anthropic.Anthropic, company: str, domain: str | None
) -> list[dict]:
    prompt = CONTACT_PROMPT.format(
        company=company, domain=domain or "not available"
    )
    messages = [{"role": "user", "content": prompt}]
    for _ in range(2):
        try:
            resp = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=CLAUDE_MAX_TOKENS,
                messages=messages,
                tools=[{
                    "type": WEB_SEARCH_TOOL_VERSION,
                    "name": "web_search",
                    "max_uses": CONTACT_MAX_SEARCHES,
                }],
                output_config={
                    "format": {"type": "json_schema", "schema": CONTACT_SCHEMA}
                },
            )
        except Exception as e:
            print(f"  Claude contact search failed for {company!r}: {e}")
            return []

        if resp.stop_reason == "pause_turn":
            messages = [
                messages[0],
                {"role": "assistant", "content": resp.content},
            ]
            continue
        if resp.stop_reason == "refusal":
            print(f"  Claude refused contact search for {company!r}")
            return []

        text = next((b.text for b in resp.content if b.type == "text"), None)
        if not text:
            return []
        try:
            return (json.loads(text) or {}).get("contacts") or []
        except json.JSONDecodeError:
            return []

    print(f"  Claude pause_turn unresolved for {company!r}")
    return []


def _amf_decision_maker(
    domain: str | None, company_name: str
) -> dict | None:
    """Call AnyMailFinder decision-maker. Returns contact dict or None.

    Tries categories in priority order per AMF: ceo (CEO/Owner/Founder),
    then operations (COO), engineering (CTO), finance (CFO), sales (CRO),
    marketing (CMO). AMF picks the first category that resolves to a
    valid email — we pay 2 credits only when one does."""
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


def discover_for_company(
    conn,
    company_id: int,
    company_name: str,
    domain: str | None,
    client: anthropic.Anthropic,
) -> int:
    """Full discovery + verification + AMF fallback for one company.
    Returns number of verified contacts saved (capped at 3)."""
    saved = 0
    print(f"Discovering contacts for {company_name!r} (domain={domain})")

    people = _claude_find_people(client, company_name, domain)
    print(f"  Claude returned {len(people)} candidate(s)")
    for person in people[:3]:
        first = (person.get("first_name") or "").strip()
        last = (person.get("last_name") or "").strip()
        title = (person.get("title") or "").strip()
        if not first or not last:
            continue
        email = _try_patterns(first, last, domain) if domain else None
        if not email:
            continue
        db.save_contact(
            conn, company_id, first, last, title,
            email, "ok", "pattern",
        )
        saved += 1

    if saved == 0:
        amf = _amf_decision_maker(domain, company_name)
        if amf:
            print(f"  AMF fallback returned: {amf['email']}")
            db.save_contact(
                conn, company_id,
                amf["first_name"], amf["last_name"], amf["title"],
                amf["email"], "ok", "amf",
            )
            saved = 1

    db.mark_company_contacts_discovered(conn, company_id)
    print(f"  Saved {saved} contact(s)")
    return saved
