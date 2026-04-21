# job-posting-lead-signal-automation

Scrapes US job postings from the last 24h across LinkedIn and Indeed for a
fixed set of data/AI roles, dedupes by company, enriches each company via
Claude Haiku 4.5 (with server-side web search) for employee count + industry,
filters out staffing/nonprofit/educational/job-platform companies and anything
over 50 employees (or unknown size), and appends the survivors to a Google
Sheet.

Designed to run as a weekday-morning cron job on Railway.

## Roles

- Data Engineer
- Analytics Engineer
- Data Analyst
- AI Engineer
- AI Consultant
- Data Consultant

## Sheets

Two sheets, same columns except the raw sheet has one extra.

**Output sheet** (`GOOGLE_SHEET_ID`) — companies that passed the filter, one
row per company. Columns:

`run_timestamp_utc, job_title, job_url, company, company_url, location, source, employee_count, industry`

**Raw sheet** (`RAW_SHEET_ID`) — every company ever seen, used as an
enrichment cache so repeat companies skip the Claude call. Same columns plus
`passed_lookup` (`Yes` / `No` / blank). One row per company (upsert).

On each run: new companies get appended to the raw sheet with blank
`passed_lookup`, `employee_count`, `industry`. The enrichment step fills those
in — `Yes` if the company meets the filters, `No` if not. On the next run,
any company already marked `Yes` or `No` in the raw sheet skips Claude
entirely; blanks get retried. Company matching across sheets uses
fuzzy matching to handle punctuation and suffix variations
("Acme, Inc." ≡ "Acme Inc").

## Local run

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in Google creds + sheet ID
python main.py
```

## Google Sheets setup

1. In Google Cloud console, create a **service account** and download its JSON
   key. Enable the **Google Sheets API** for that project.
2. Create two sheets (output and raw) and **share both** with the service
   account email (`...@...iam.gserviceaccount.com`) as an Editor.
3. Copy each sheet ID from the URL
   (`docs.google.com/spreadsheets/d/<THIS>/edit`) into `GOOGLE_SHEET_ID` and
   `RAW_SHEET_ID`.
4. Paste the full service account JSON (single line) into
   `GOOGLE_SERVICE_ACCOUNT_JSON`.

The first run writes a header row to each sheet if empty. The output sheet is
append-only. The raw sheet gets new rows appended and existing rows updated
in place (the `passed_lookup` / `employee_count` / `industry` cells).

## Railway deploy

1. Push this repo to GitHub.
2. On Railway: **New Project → Deploy from GitHub repo** → select this repo.
3. In **Variables**, set `GOOGLE_SERVICE_ACCOUNT_JSON`, `GOOGLE_SHEET_ID`,
   `RAW_SHEET_ID`, `ANTHROPIC_API_KEY`, and optionally `GOOGLE_WORKSHEET_NAME`
   / `RAW_WORKSHEET_NAME`.
4. In **Settings → Cron Schedule**, set your weekday-morning schedule.
5. Deploy.
