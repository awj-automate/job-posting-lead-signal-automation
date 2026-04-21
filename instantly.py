"""
Sync verified contacts to an Instantly campaign.

Pulls contacts where `email_status = 'ok'` and `synced_to_instantly_at IS NULL`,
then POSTs them in batches of up to 1000 to Instantly's bulk leads endpoint.
Stamps `synced_to_instantly_at` + `instantly_lead_id` for each successfully
created lead (mapped back via the response's `index` field).
"""

import os

import requests

import db


ENDPOINT = "https://api.instantly.ai/api/v2/leads/add"
BATCH_SIZE = 1000
HTTP_TIMEOUT = 60


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def sync_unsynced(conn) -> int:
    """Sync all unsynced ok-verified contacts. Returns count synced."""
    contacts = db.load_unsynced_contacts(conn)
    if not contacts:
        print("Instantly: no unsynced contacts.")
        return 0

    campaign_id = os.environ["INSTANTLY_CAMPAIGN_ID"]
    headers = {
        "Authorization": f"Bearer {os.environ['INSTANTLY_API_KEY']}",
        "Content-Type": "application/json",
    }

    total_synced = 0
    for batch in _chunks(contacts, BATCH_SIZE):
        payload = {
            "campaign_id": campaign_id,
            "leads": [
                {
                    "email": row["email"],
                    "first_name": row["first_name"] or "",
                    "last_name": row["last_name"] or "",
                    "company_name": row["company_name"] or "",
                    "job_title": row["title"] or "",
                }
                for row in batch
            ],
            "skip_if_in_workspace": True,
        }
        try:
            resp = requests.post(
                ENDPOINT, headers=headers, json=payload, timeout=HTTP_TIMEOUT
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            print(f"Instantly: batch failed: {e}")
            continue

        for created in data.get("created_leads") or []:
            idx = created.get("index")
            lead_id = created.get("id")
            if idx is None or lead_id is None or idx >= len(batch):
                continue
            db.mark_contact_synced(conn, batch[idx]["id"], lead_id)
            total_synced += 1
        print(
            f"Instantly: batch of {len(batch)} sent — "
            f"{len(data.get('created_leads') or [])} created, "
            f"{data.get('skipped_count', 0)} skipped, "
            f"{data.get('duplicated_leads', 0)} duplicated"
        )

    return total_synced
