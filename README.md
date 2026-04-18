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

## Output columns

Appended to the configured Google Sheet:

`run_timestamp_utc, job_title, job_url, company, company_url, location, source, employee_count, industry`

## Local run

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in Google creds + sheet ID
python main.py
```

## Google Sheets setup

1. In Google Cloud console, create a **service account** and download its JSON
   key. Enable the **Google Sheets API** for that project.
2. Open the target sheet and **share it** with the service account email
   (`...@...iam.gserviceaccount.com`) as an Editor.
3. Copy the sheet ID from the URL
   (`docs.google.com/spreadsheets/d/<THIS>/edit`) into `GOOGLE_SHEET_ID`.
4. Paste the full service account JSON (single line) into
   `GOOGLE_SERVICE_ACCOUNT_JSON`.

The first run writes a header row if the sheet is empty. Subsequent runs
append only — existing rows are never touched.

## Railway deploy

1. Push this repo to GitHub.
2. On Railway: **New Project → Deploy from GitHub repo** → select this repo.
3. In **Variables**, set `GOOGLE_SERVICE_ACCOUNT_JSON`, `GOOGLE_SHEET_ID`,
   `ANTHROPIC_API_KEY`, and optionally `GOOGLE_WORKSHEET_NAME`.
4. In **Settings → Cron Schedule**, set your weekday-morning schedule.
5. Deploy.
