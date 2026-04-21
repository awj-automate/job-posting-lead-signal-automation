"""
Google Sheets I/O and company-name matching.

Two sheets:
- Output sheet: companies that passed the filter. Canonical result log.
- Raw sheet: every company seen, with a `passed_lookup` column used as a
  per-company enrichment cache across runs. One row per company (upsert).
"""

import json
import os
import re
from dataclasses import dataclass
from typing import Iterable

import gspread
from google.oauth2.service_account import Credentials
from rapidfuzz import fuzz, process


SHEET_COLUMNS = [
    "run_timestamp_utc",
    "job_title",
    "job_url",
    "company",
    "company_url",
    "location",
    "source",
    "employee_count",
    "industry",
]
RAW_SHEET_COLUMNS = SHEET_COLUMNS + ["passed_lookup"]

FUZZY_THRESHOLD = 90

_PUNCT_RE = re.compile(r"[^\w\s]")
_WS_RE = re.compile(r"\s+")
# Common corporate suffixes stripped from the trailing end of the name.
_SUFFIX_RE = re.compile(
    r"\b(incorporated|inc|llc|l l c|ltd|limited|corp|corporation|co|company|"
    r"plc|gmbh|sa|nv|bv|holdings|holding|group|llp|lp|pc|pllc)\s*$",
    re.IGNORECASE,
)


def normalize_company(name: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace, drop corp suffixes."""
    if not name:
        return ""
    s = _PUNCT_RE.sub(" ", name.lower())
    s = _WS_RE.sub(" ", s).strip()
    prev = None
    while s != prev:
        prev = s
        s = _SUFFIX_RE.sub("", s).strip()
    return _WS_RE.sub(" ", s).strip()


def fuzzy_find(key: str, candidates: Iterable[str]) -> str | None:
    """Return the best-matching candidate at/above threshold, else None.

    Candidates must already be normalized. Exact match wins immediately.
    """
    if not key:
        return None
    candidates = list(candidates)
    if not candidates:
        return None
    if key in candidates:
        return key
    match = process.extractOne(
        key, candidates, scorer=fuzz.ratio, score_cutoff=FUZZY_THRESHOLD
    )
    return match[0] if match else None


@dataclass
class RawRow:
    row_index: int  # 1-based, includes header row
    company: str
    normalized: str
    passed_lookup: str  # "Yes", "No", or ""
    employee_count: str
    industry: str


class SheetClient:
    def __init__(self):
        creds_json = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
        creds = Credentials.from_service_account_info(
            json.loads(creds_json),
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        self.gc = gspread.authorize(creds)
        self._output_ws = None
        self._raw_ws = None
        self._raw_row_count = 0  # total rows in raw sheet incl. header

    @property
    def output_ws(self):
        if self._output_ws is None:
            sh = self.gc.open_by_key(os.environ["GOOGLE_SHEET_ID"])
            self._output_ws = sh.worksheet(
                os.environ.get("GOOGLE_WORKSHEET_NAME", "Sheet1")
            )
        return self._output_ws

    @property
    def raw_ws(self):
        if self._raw_ws is None:
            sh = self.gc.open_by_key(os.environ["RAW_SHEET_ID"])
            self._raw_ws = sh.worksheet(
                os.environ.get("RAW_WORKSHEET_NAME", "Sheet1")
            )
        return self._raw_ws

    @staticmethod
    def _ensure_header(ws, columns: list[str]) -> list[list[str]]:
        values = ws.get_all_values()
        if not values:
            ws.append_row(columns, value_input_option="USER_ENTERED")
            return [columns]
        return values

    def load_output_company_keys(self) -> set[str]:
        """Normalized company keys already present in the output sheet."""
        values = self._ensure_header(self.output_ws, SHEET_COLUMNS)
        if len(values) < 2 or "company" not in values[0]:
            return set()
        col = values[0].index("company")
        return {
            normalize_company(row[col])
            for row in values[1:]
            if col < len(row) and row[col].strip()
        }

    def load_raw_cache(self) -> dict[str, RawRow]:
        """{normalized_key: RawRow} for every company in the raw sheet."""
        values = self._ensure_header(self.raw_ws, RAW_SHEET_COLUMNS)
        self._raw_row_count = len(values)
        if len(values) < 2:
            return {}
        header = values[0]
        idx = {c: i for i, c in enumerate(header)}

        def cell(row, col):
            j = idx.get(col)
            return row[j] if j is not None and j < len(row) else ""

        cache: dict[str, RawRow] = {}
        for i, row in enumerate(values[1:], start=2):
            key = normalize_company(cell(row, "company"))
            if not key:
                continue
            cache[key] = RawRow(
                row_index=i,
                company=cell(row, "company"),
                normalized=key,
                passed_lookup=cell(row, "passed_lookup"),
                employee_count=cell(row, "employee_count"),
                industry=cell(row, "industry"),
            )
        return cache

    def append_raw_rows(self, rows: list[list[str]]) -> list[int]:
        """Append rows to the raw sheet, return their 1-based row indices."""
        if not rows:
            return []
        start = self._raw_row_count + 1
        self.raw_ws.append_rows(rows, value_input_option="USER_ENTERED")
        self._raw_row_count += len(rows)
        return list(range(start, start + len(rows)))

    def update_raw_lookups(
        self,
        updates: list[tuple[int, str, str, str]],
    ) -> None:
        """Batch-write (passed_lookup, employee_count, industry) per row_index."""
        if not updates:
            return
        header = self.raw_ws.row_values(1)
        col = {c: header.index(c) + 1 for c in header}
        fields = ("passed_lookup", "employee_count", "industry")
        data = []
        for row_index, passed, emp, ind in updates:
            vals = {"passed_lookup": passed, "employee_count": emp, "industry": ind}
            for f in fields:
                if f in col:
                    data.append(
                        {
                            "range": gspread.utils.rowcol_to_a1(row_index, col[f]),
                            "values": [[vals[f]]],
                        }
                    )
        self.raw_ws.batch_update(data, value_input_option="USER_ENTERED")

    def append_output_rows(self, rows: list[list[str]]) -> None:
        if not rows:
            return
        self._ensure_header(self.output_ws, SHEET_COLUMNS)
        self.output_ws.append_rows(rows, value_input_option="USER_ENTERED")
