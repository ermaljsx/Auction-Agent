"""Locale-aware parsing helpers shared by adapters (French number and date formats)."""
from __future__ import annotations

import re
from datetime import date, datetime, time
from typing import Optional
from zoneinfo import ZoneInfo

PARIS = ZoneInfo("Europe/Paris")
MAX_MILEAGE_KM = 1_500_000

_SPACE = "[   ]"
_SEP = "[   .]"  # French thousands separators: spaces, and sometimes '.'
_GROUPED = rf"\d{{1,3}}(?:{_SEP}\d{{3}})+"
_LEFT = r"(?<![\d.,])"  # never start in the middle of a number
_KM_LINE_RE = re.compile(rf"^\s*({_GROUPED}|\d+)\s*km\s*$", re.IGNORECASE)
_KM_ANY_RE = re.compile(rf"{_LEFT}({_GROUPED}|\d+)\s*km\b", re.IGNORECASE)
_EUR_RE = re.compile(rf"{_LEFT}({_GROUPED}|\d+)(?:[.,](\d{{1,2}}))?\s*€")
_FIRST_INT_RE = re.compile(rf"{_LEFT}({_GROUPED}|\d+)(?![.,]\d)")

MONTHS_FR = {
    "janvier": 1, "fevrier": 2, "février": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6,
    "juillet": 7, "aout": 8, "août": 8, "septembre": 9, "octobre": 10, "novembre": 11,
    "decembre": 12, "décembre": 12,
}
# Short forms, as the Alcopa room page prints its sale cards ('lun. 9 nov.', seen on
# 2026-11-07): the usual French abbreviations, with and without accents. 'mars', 'mai',
# 'juin' and 'août' have none; 'mar' is left out on purpose, since it also abbreviates mardi.
MONTHS_FR_SHORT = {
    "janv": 1, "jan": 1, "févr": 2, "fevr": 2, "fév": 2, "fev": 2, "avr": 4, "juil": 7,
    "sept": 9, "sep": 9, "oct": 10, "nov": 11, "déc": 12, "dec": 12,
}


def month_fr(token: Optional[str]) -> int:
    """'novembre', 'nov' or 'Nov.' → 11; anything else → 0."""
    key = (token or "").strip().rstrip(".").lower()
    return MONTHS_FR.get(key) or MONTHS_FR_SHORT.get(key, 0)


def _digits(token: str) -> int:
    return int(re.sub(r"\D", "", token))


def bounded_km(value: Optional[int]) -> Optional[int]:
    if value is None or value < 0 or value > MAX_MILEAGE_KM:
        return None
    return value


def parse_km_lines(lines: list[str]) -> Optional[int]:
    """Mileage from a list of text lines, one line at a time (never across lines)."""
    for line in lines:
        m = _KM_LINE_RE.match(line)
        if m:
            return bounded_km(_digits(m.group(1)))
    return None


def parse_km(text: Optional[str]) -> Optional[int]:
    """First mileage token in a single field value such as '18 437 KM'."""
    if not text:
        return None
    m = _KM_ANY_RE.search(text.replace("\n", " | "))
    return bounded_km(_digits(m.group(1))) if m else None


def parse_eur(text: Optional[str]) -> Optional[float]:
    """First euro amount: '10 200 €' → 10200.0, '10.200 €' → 10200.0, '--' → None."""
    if not text or "€" not in text:
        return None
    m = _EUR_RE.search(text)
    if not m:
        return None
    value = float(_digits(m.group(1)))
    if m.group(2):
        value += int(m.group(2)) / (10 ** len(m.group(2)))
    return value


def parse_plain_amount(text: Optional[str]) -> Optional[float]:
    """An attribute value like '10200' or '10200.00'."""
    if text is None:
        return None
    cleaned = str(text).strip().replace(" ", "").replace(" ", "")
    if not cleaned or cleaned in {"--", "-"}:
        return None
    try:
        return float(cleaned.replace(",", "."))
    except ValueError:
        return None


def first_int(text: Optional[str]) -> Optional[int]:
    """First integer token: '1598 cm3' → 1598 (never concatenates separate numbers)."""
    if not text:
        return None
    m = _FIRST_INT_RE.search(text)
    return _digits(m.group(1)) if m else None


def parse_fr_date(text: Optional[str]) -> Optional[date]:
    """'19 octobre 2026', '9 nov. 2026' or '19/10/2026' → date."""
    if not text:
        return None
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", text)
    if m:
        d, mth, y = (int(x) for x in m.groups())
    else:
        m = re.search(r"(\d{1,2})(?:er)?\s+([a-zéûè]+)\.?\s+(\d{4})", text, re.IGNORECASE)
        if not m:
            return None
        d, y = int(m.group(1)), int(m.group(3))
        mth = month_fr(m.group(2))
    try:
        return date(y, mth, d)
    except ValueError:
        return None


def paris_datetime(day: date, hour: int = 0, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=PARIS)
