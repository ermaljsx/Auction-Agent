"""Parser for French Contrôle Technique (CT) procès-verbal text, version 2.

What changed versus the prototype (see docs/MASTER_PLAN.md, critique row 3):
- Text is accent-folded (NFKD, combining marks removed) and upper-cased before
  matching, so NFD-encoded PDFs and plural headers ("DÉFAILLANCES MAJEURES") work.
- The overall result is read from the "Résultat" field when present. Keyword counting
  is only a fallback, and an unknown result stays UNKNOWN instead of being guessed.
- Only lines that start with a defect code (e.g. ``1.1.13.a.2``) count as defects.
- Severity comes from the section header. The code's last digit (1/2/3) is recorded as
  a cross-check only, because its meaning is confirmed by a single secondary source.

The layouts of real Alcopa PVs are not yet verified (Phase 2 recon), so every result
carries a ``parse_confidence`` and scoring treats low confidence as "CT non lu".
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date
from typing import Any, Optional

PARSER_VERSION = "ct-2.0"

RESULT_FAVORABLE = "FAVORABLE"
RESULT_MAJOR = "DEFAVORABLE_MAJEURE"
RESULT_CRITICAL = "DEFAVORABLE_CRITIQUE"
RESULT_UNKNOWN = "UNKNOWN"

SEVERITIES = ("minor", "major", "critical")

SYSTEMS = {
    "0": "identification",
    "1": "freinage",
    "2": "direction",
    "3": "visibilite",
    "4": "eclairage",
    "5": "liaisons_sol",
    "6": "structure",
    "7": "equipements",
    "8": "nuisances",
    "9": "supplementaires",
}
CRITICAL_SYSTEMS = {"freinage", "direction", "structure"}

_HEADER_RE = re.compile(r"^\s*DEFAILLANCES?\s+(MINEURES?|MAJEURES?|CRITIQUES?)\b(.*)$")
_NONE_RE = re.compile(r"\b(NEANT|AUCUNE?|SANS OBJET)\b")
_CODE_RE = re.compile(
    r"^\s*(?P<code>(?P<fn>\d)\.\d{1,2}(?:\.\d{1,2}){0,2}(?:\.[A-Z])?(?:\.(?P<suffix>[123]))?)"
    r"(?![\d.])\s*[-:.)]?\s*(?P<label>.*)$"
)
_FOOTER_RE = re.compile(
    r"^\s*(PROCHAIN|DATE|CENTRE|SIGNATURE|VALIDITE|KILOMETRAGE|RESULTAT|NATURE|OBSERVATIONS?"
    r"|COMMENTAIRES?|LES DEFAILLANCES|CONTROLEUR|AGREMENT|PROCES[- ]VERBAL|PAGE\s+\d)\b"
)
_RESULT_FIELD_RE = re.compile(r"RESULTAT(?:\s+DU\s+CONTROLE)?\s*[:\-]?\s*(?P<value>[^\n]{0,120})")
_PHRASES = {
    RESULT_CRITICAL: re.compile(r"DEFAVORABLE\s+POUR\s+(?:LES\s+)?DEFAILLANCES?\s+CRITIQUES?"),
    RESULT_MAJOR: re.compile(r"DEFAVORABLE\s+POUR\s+(?:LES\s+)?DEFAILLANCES?\s+MAJEURES?"),
}
_DATE_LABEL_RE = re.compile(
    r"DATE\s+(?:DU\s+CONTROLE|DE\s+(?:LA\s+)?VISITE|D'?INSPECTION|DU\s+PASSAGE)\s*[:\-]?\s*"
    r"(\d{2})[/.\-](\d{2})[/.\-](\d{4})"
)
_KM_LABEL_RE = re.compile(
    r"KILOMETRAGE(?:\s+(?:RELEVE|AU\s+COMPTEUR|LU|ACTUEL))?\s*[:\-]?\s*"
    r"(\d{1,3}(?:[ .]\d{3})+|\d{1,7})\s*KM\b"
)
_ODO_PAIR_RE = re.compile(r"(\d{2})/(\d{2})/(\d{4})[^\n\d]{0,25}?(\d{1,3}(?:[ .]\d{3})+|\d{4,7})\s*KM\b")


def fold(text: str) -> str:
    """Upper-case, strip accents and normalise spaces, keeping line breaks."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    stripped = stripped.replace(" ", " ").replace(" ", " ")
    return stripped.upper()


def _severity_from_header(word: str) -> str:
    if word.startswith("MINEURE"):
        return "minor"
    if word.startswith("MAJEURE"):
        return "major"
    return "critical"


def _suffix_severity(suffix: Optional[str]) -> Optional[str]:
    return {"1": "minor", "2": "major", "3": "critical"}.get(suffix or "")


def _to_int(token: str) -> int:
    return int(re.sub(r"[ .]", "", token))


def _date(d: str, m: str, y: str) -> Optional[str]:
    try:
        return date(int(y), int(m), int(d)).isoformat()
    except ValueError:
        return None


def _detect_nature(folded: str) -> str:
    nature_line = re.search(r"NATURE\s+DU\s+CONTROLE\s*[:\-]?\s*([^\n]{0,80})", folded)
    scope = nature_line.group(1) if nature_line else folded
    if "VOLONTAIRE" in scope:
        return "volontaire"
    if nature_line and "CONTRE" in scope and "VISITE" in scope:
        return "contre_visite"
    if "PERIODIQUE" in scope:
        return "periodique"
    return "unknown"


def _result_from_field(folded: str) -> Optional[str]:
    for match in _RESULT_FIELD_RE.finditer(folded):
        value = match.group("value")
        has_fav = re.search(r"\bFAVORABLE\b", value) is not None
        has_def = "DEFAVORABLE" in value
        if (has_fav and has_def) or ("MAJEURE" in value and "CRITIQUE" in value):
            continue  # a legend listing every possible result, not the result itself
        if has_def:
            if "CRITIQUE" in value:
                return RESULT_CRITICAL
            if "MAJEURE" in value:
                return RESULT_MAJOR
            return None  # unfavourable but severity unreadable: decide from defects
        if has_fav:
            return RESULT_FAVORABLE
    return None


def parse_controle_technique(text: Optional[str]) -> dict[str, Any]:
    """Return a structured summary of a CT report. Never raises on odd input."""
    summary: dict[str, Any] = {
        "parser_version": PARSER_VERSION,
        "result": RESULT_UNKNOWN,
        "result_source": "none",
        "nature": "unknown",
        "inspection_date": None,
        "inspection_km": None,
        "defects": [],
        "counts": {s: 0 for s in SEVERITIES},
        "odometer": [],
        "odometer_inconsistent": False,
        "suffix_disagreements": 0,
        "parse_confidence": 0.0,
    }
    if not text or len(text.strip()) < 40:
        return summary

    original_lines = text.splitlines()
    folded_lines = [fold(line) for line in original_lines]
    folded = "\n".join(folded_lines)

    summary["nature"] = _detect_nature(folded)

    m = _DATE_LABEL_RE.search(folded)
    if m:
        summary["inspection_date"] = _date(*m.groups())
    m = _KM_LABEL_RE.search(folded)
    if m:
        summary["inspection_km"] = _to_int(m.group(1))

    # --- defects, by section. Without any section header, fall back to the code suffix.
    has_headers = any(_HEADER_RE.match(line) for line in folded_lines)
    current: Optional[str] = None
    last_defect: Optional[dict[str, Any]] = None
    for orig, line in zip(original_lines, folded_lines):
        header = _HEADER_RE.match(line)
        if header:
            current = _severity_from_header(header.group(1))
            last_defect = None
            rest = header.group(2)
            if _NONE_RE.search(rest):
                current = None
            continue
        if not line.strip():
            continue
        code = _CODE_RE.match(line)
        if code:
            suffix_sev = _suffix_severity(code.group("suffix"))
            severity = current if has_headers else suffix_sev
            if severity is None:
                last_defect = None
                continue
            if current and suffix_sev and suffix_sev != current:
                summary["suffix_disagreements"] += 1
            orig_code = _CODE_RE.match(fold(orig))
            label = orig.strip()
            if orig_code:
                start = len(orig) - len(orig.lstrip())
                label = orig[start + len(orig_code.group("code")):].strip(" -:.)\t")
            defect = {
                "code": code.group("code").lower(),
                "severity": severity,
                "system": SYSTEMS.get(code.group("fn"), "autre"),
                "label": label[:200],
            }
            summary["defects"].append(defect)
            summary["counts"][severity] += 1
            last_defect = defect
            continue
        if _FOOTER_RE.match(line):
            current = None
            last_defect = None
            continue
        if last_defect is not None and len(last_defect["label"]) < 200:
            last_defect["label"] = (last_defect["label"] + " " + orig.strip())[:200].strip()

    # --- overall result
    field_result = _result_from_field(folded)
    counts = summary["counts"]
    confidence = 1.0
    if field_result:
        summary["result"], summary["result_source"] = field_result, "field"
    else:
        phrases = [r for r, rx in _PHRASES.items() if rx.search(folded)]
        favourable_only = re.search(r"\bFAVORABLE\b", folded) and "DEFAVORABLE" not in folded
        if len(phrases) == 1:
            summary["result"], summary["result_source"] = phrases[0], "phrase"
            confidence = 0.8
        elif counts["critical"]:
            summary["result"], summary["result_source"] = RESULT_CRITICAL, "defects"
            confidence = 0.7
        elif counts["major"]:
            summary["result"], summary["result_source"] = RESULT_MAJOR, "defects"
            confidence = 0.7
        elif favourable_only:
            summary["result"], summary["result_source"] = RESULT_FAVORABLE, "phrase"
            confidence = 0.8
        else:
            confidence = 0.3

    # Defects that contradict the stated result lower confidence (and are kept).
    if summary["result"] == RESULT_FAVORABLE and (counts["major"] or counts["critical"]):
        confidence = min(confidence, 0.5)
    if summary["result"] == RESULT_MAJOR and counts["critical"]:
        confidence = min(confidence, 0.5)
    confidence -= 0.1 * summary["suffix_disagreements"]
    if summary["defects"] and not has_headers:
        confidence = min(confidence, 0.7)
    if summary["nature"] == "volontaire":
        confidence = min(confidence, 0.6)

    # --- odometer history
    pairs = []
    for d, mth, y, km in _ODO_PAIR_RE.findall(folded):
        iso = _date(d, mth, y)
        if iso:
            pairs.append((iso, _to_int(km)))
    pairs = sorted(set(pairs))
    summary["odometer"] = [{"date": d, "km": k} for d, k in pairs]
    summary["odometer_inconsistent"] = any(
        later[1] < earlier[1] for earlier, later in zip(pairs, pairs[1:])
    )

    summary["parse_confidence"] = round(max(0.0, min(1.0, confidence)), 2)
    return summary
