"""Battery-certificate parser (traction battery state of health).

One layout is known: the Moba "Certificat batterie" (Batteries For People) that Alcopa
links as ``/getDocument/moba/…`` for electric and plug-in hybrid cars. Its first page has
a vehicle block (diagnostic date, mileage), a battery block where the state of health
(SoH) stands alone on its own line ("95,35%") above a percentile line ("50% 50%") and the
caption "État de Santé (SOH)", the usable and new capacity, and the manufacturer's usual
battery warranty.

The summary holds no VIN, plate, model line, link, company number or place: only the
values below. Like the CT parser it never raises; an internal error gives an empty
summary with a ``parse_error:<Exception>`` warning.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any, Optional

from .ct_parser import fold

PARSER_VERSION = "bat-1.0"

SOH_MIN, SOH_MAX = 30.0, 105.0
SOH_CAPACITY_TOLERANCE = 1.5  # points between the printed SoH and capacity ratio

_LAYOUT_RE = re.compile(r"CERTIFICAT\s+BATTERIE|\bSOH\b")
_PROVIDER_MOBA_RE = re.compile(r"\bMOBA\b|BATTERIES\s+FOR\s+PEOPLE")
_SOH_START_RE = re.compile(r"ETAT\s+DE\s+LA\s+BATTERIE|COMPARAISON\s+DU\s+SOH")
_SOH_CAPTION_RE = re.compile(r"ETAT\s+DE\s+SANTE\s*\(SOH\)")
_SOH_LINE_RE = re.compile(r"^(\d{1,3}(?:[.,]\d{1,2})?)\s*%$")
_CAPACITY_RE = re.compile(
    r"CAPACITE\s+UTILE\s+RESTANTE\s*/\s*CAPACITE\s+UTILE\s+NEUVE(?:\s*\(\d\))?\s*"
    r"(\d{1,4}(?:[.,]\d{1,3})?)\s*KWH\s*/\s*(\d{1,4}(?:[.,]\d{1,3})?)\s*KWH"
)
_DATE_RE = re.compile(r"DATE\s+DU\s+DIAGNOSTIC\s*:\s*(\d{2})/(\d{2})/(\d{4})")
_KM_RE = re.compile(r"^KILOMETRAGE\s*(?:\(\d\))?\s*:\s*(\d{1,3}(?:[ .]\d{3})+|\d{1,7})\s*KM\b", re.MULTILINE)
_KM_DECLARED_RE = re.compile(r"SELON\s+DECLARATION")
_WARRANTY_RE = re.compile(
    r"SOH\s+SUPERIEUR\s+A\s+(\d{2})\s*%\s*,?\s*PENDANT\s+(\d{1,2})\s+ANS\s+OU\s+(\d{1,3}(?:[ .]\d{3})+|\d{1,7})\s*KM"
)


def _empty_summary() -> dict[str, Any]:
    return {
        "parser_version": PARSER_VERSION,
        "provider": "unknown",
        "soh_pct": None,
        "soh_source": None,  # "certificate" (the printed SoH) | "capacity_ratio"
        "capacity_kwh": None,
        "capacity_new_kwh": None,
        "capacity_ratio_pct": None,
        "diagnostic_date": None,
        "diagnostic_km": None,
        "km_declared": False,
        "warranty_hint": None,
        "warnings": [],
        "parse_confidence": 0.0,
    }


def parse_battery_certificate(text: Optional[str]) -> dict[str, Any]:
    """Return the certificate summary. Never raises on odd input."""
    summary = _empty_summary()
    try:
        _parse_into(summary, text or "")
    except Exception as exc:  # defensive: an odd text must never break an ingest or a rescore
        summary = _empty_summary()
        summary["warnings"] = [f"parse_error:{exc.__class__.__name__}"]
    return summary


def _number(raw: str) -> float:
    return float(raw.replace(" ", "").replace(",", "."))


def _int(raw: str) -> int:
    return int(re.sub(r"[ .]", "", raw))


def _soh_line(lines: list[str]) -> Optional[str]:
    """The SoH printed alone on a line inside the battery block, never the percentile line
    ("50% 50%") or the warranty ("SOH supérieur à 70%")."""
    start = next((i for i, line in enumerate(lines) if _SOH_START_RE.search(line)), None)
    caption = next((i for i, line in enumerate(lines) if _SOH_CAPTION_RE.search(line)), None)
    if start is None and caption is None:
        return None
    if start is None:
        start = max(0, caption - 12)
    end = caption + 3 if caption is not None and caption >= start else start + 12
    for line in lines[start:end]:
        m = _SOH_LINE_RE.match(line)
        if m:
            return m.group(1)
    return None


def _parse_into(summary: dict[str, Any], text: str) -> None:
    lines = [re.sub(r"\s+", " ", line).strip() for line in fold(text).splitlines()]
    folded = "\n".join(lines)
    warnings: list[str] = summary["warnings"]

    if _PROVIDER_MOBA_RE.search(folded):
        summary["provider"] = "moba"

    m = _DATE_RE.search(folded)
    if m:
        try:
            summary["diagnostic_date"] = date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()
        except ValueError:
            warnings.append("bad_diagnostic_date")
    m = _KM_RE.search(folded)
    if m:
        summary["diagnostic_km"] = _int(m.group(1))
    summary["km_declared"] = bool(_KM_DECLARED_RE.search(folded))
    m = _WARRANTY_RE.search(folded)
    if m:
        summary["warranty_hint"] = {"soh_min_pct": int(m.group(1)), "years": int(m.group(2)),
                                    "km": _int(m.group(3))}

    ratio = None
    m = _CAPACITY_RE.search(folded)
    if m:
        usable, new = _number(m.group(1)), _number(m.group(2))
        summary["capacity_kwh"], summary["capacity_new_kwh"] = usable, new
        if new > 0:
            ratio = round(100 * usable / new, 2)
            summary["capacity_ratio_pct"] = ratio

    soh = None
    raw = _soh_line(lines)
    if raw is not None:
        soh = _number(raw)
        if not SOH_MIN <= soh <= SOH_MAX:
            warnings.append("soh_out_of_range")
            soh = None

    if soh is not None:
        summary["soh_pct"], summary["soh_source"] = soh, "certificate"
        if ratio is None:
            summary["parse_confidence"] = 0.9
        elif abs(soh - ratio) <= SOH_CAPACITY_TOLERANCE:
            summary["parse_confidence"] = 1.0
        else:
            warnings.append("soh_capacity_mismatch")
            summary["parse_confidence"] = 0.6
    elif ratio is not None and SOH_MIN <= ratio <= SOH_MAX:
        # The certificate's own fallback (its footnote): usable capacity over new capacity.
        summary["soh_pct"], summary["soh_source"] = ratio, "capacity_ratio"
        summary["parse_confidence"] = 0.85
    else:
        warnings.append("soh_not_found" if _LAYOUT_RE.search(folded) else "unknown_layout")
        summary["parse_confidence"] = 0.0
