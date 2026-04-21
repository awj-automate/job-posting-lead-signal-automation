# job-posting-lead-signal-automation

End-to-end lead pipeline:

1. Scrape US job postings from the last 24h across LinkedIn and Indeed for a
   fixed set of data/AI roles.
2. Dedupe by company and enrich each via Claude Haiku 4.5 (web search) —
   employee count, industry, domain, rejection flags. Drop
   staffing/nonprofit/educational/job-platform companies and anything over 50
   employees.
3. For each passing company, find up to 3 C-level decision makers via Claude
   web search, generate common email patterns, verify with MillionVerifier,
   and fall back to AnyMailFinder's decision-maker endpoint if the cheap path
   finds zero.
4. Push verified contacts into an Instantly campaign.

All state lives in Neon Postgres. Designed to run as a weekday-morning cron
job on Railway.

## Roles

- Data Engineer
- Analytics Engineer
- Data Analyst
- AI Engineer
- AI Consultant
- Data Consultant

## Data model (Neon Postgres)

| Table | Role |
|---|---|
| `companies` | One row per unique company (fuzzy-matched by normalized name). Holds the enrichment cache: `passed_lookup`, `employee_count`, `industry`, `rejection_reason`. |
| `jobs` | Every scraped posting, linked to its company. Historical record. |
| `contacts` | People discovered at passing companies. Populated by Phase 2. |
| `runs` | One row per cron invocation with counts + error text. Audit log. |

Schema lives in `schema.sql` and is applied idempotently on every startup.

## Flow

1. Apply schema (no-op if tables already exist).
2. Scrape → within-run fuzzy dedupe.
3. Load the companies cache from Neon. Classify each scraped row:
   - cached `yes` or `no` → record the job, skip enrichment.
   - cached `null` (lookup pending) or new company → queue for enrichment.
4. Enrich the queued companies via Claude. Update their rows with
   `passed_lookup`, a rejection reason where applicable, and domain.
5. For each passing company whose `contacts_discovered_at` is null, run the
   contact discovery pass (capped at 50/run): Claude finds up to 3 C-level
   candidates, patterns get verified with MillionVerifier, and AnyMailFinder
   decision-maker is the fallback when the cheap path returns zero.
6. Push all `ok`-verified contacts with `synced_to_instantly_at IS NULL`
   to the Instantly campaign in batches of 1000.
7. Record counts + `finished_at` on the `runs` row.

### Cost per new passing company (rough)

| Step | ~Cost |
|---|---|
| Enrichment (Claude Haiku 4.5 + web search) | ~$0.002 |
| Contact discovery (Claude Haiku 4.5 + web search) | ~$0.002 |
| MillionVerifier (up to ~24 candidates = 3 people × 8 patterns) | ~$0.05 |
| AnyMailFinder fallback (only if cheap path finds zero) | 2 credits (~$0.01–0.05) |
| **Total** | **~$0.05–0.10 per company** |

## Local run

Not supported — this project runs on Railway. Local development would require
Neon creds and scraping rate-limits that aren't worth working around.

## Env vars

| Var | Purpose |
|---|---|
| `DATABASE_URL` | Neon Postgres connection string (pooled, with `?sslmode=require`). |
| `ANTHROPIC_API_KEY` | Claude Haiku 4.5 for enrichment + contact discovery. |
| `MILLIONVERIFIER_API_KEY` | Verifies pattern-generated emails. |
| `ANYMAILFINDER_API_KEY` | Decision-maker fallback when patterns fail. |
| `INSTANTLY_API_KEY` | Push verified contacts to an Instantly campaign. |
| `INSTANTLY_CAMPAIGN_ID` | The campaign UUID to sync contacts into. |

## Railway deploy

1. Push this repo to GitHub.
2. On Railway: **New Project → Deploy from GitHub repo** → select this repo.
3. Create a Neon database (pick the same region as your Railway service),
   grab the pooled connection string, set it as `DATABASE_URL`.
4. Set remaining variables (see table above).
5. In **Settings → Cron Schedule**, set your weekday-morning schedule.
6. Deploy.
