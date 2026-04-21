"""Company-name normalization and fuzzy matching."""

import re
from typing import Iterable

from rapidfuzz import fuzz, process


FUZZY_THRESHOLD = 90

_PUNCT_RE = re.compile(r"[^\w\s]")
_WS_RE = re.compile(r"\s+")
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

    Candidates must already be normalized. Exact match short-circuits.
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
