"""Deterministic scoring rubric (zero cost, fully reproducible).

score = 0.35·R + 0.35·C + 0.30·V (+ bounded LLM adjustment), then caps from gates.
- R (reliability) and C (condition) never depend on price.
- V (value) is computed from the all-in effective price against a cohort of comparable
  cars; with too few comparables it is neutral (50) and the result is "partial".
- Gates express a maximum tier. A partial result is capped at yellow.
Everything is recomputed on every scoring run, so ages, CT freshness, cohorts and
thresholds can never go stale. See docs/MASTER_PLAN.md §4.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path
from statistics import median
from typing import Any, Callable, Optional

from ..config import Settings
from ..ct_parser import CRITICAL_SYSTEMS, RESULT_CRITICAL, RESULT_FAVORABLE, RESULT_MAJOR, fold

HEURISTIC_VERSION = "h1-2026.11"
WEIGHTS = {"R": 0.35, "C": 0.35, "V": 0.30}
TIER_ORDER = {"red": 0, "yellow": 1, "green": 2}

REPAIR_COST_MAJOR = {
    "freinage": 250, "direction": 300, "structure": 600, "liaisons_sol": 250, "eclairage": 80,
    "visibilite": 120, "nuisances": 300, "equipements": 150, "identification": 50,
}
CT_REDO_COST = 80.0

# French names of the CT functions (ct_parser.SYSTEMS slugs), for gates and the site.
SYSTEM_FR = {
    "identification": "Identification",
    "freinage": "Freinage",
    "direction": "Direction",
    "visibilite": "Visibilité",
    "eclairage": "Feux et équipements électriques",
    "liaisons_sol": "Essieux, roues, pneus, suspension",
    "structure": "Châssis et carrosserie",
    "equipements": "Autres équipements",
    "nuisances": "Nuisances (pollution, bruit)",
    "supplementaires": "Contrôles supplémentaires",
    "vehicule_ecole": "Véhicule école",
    "autre": "Autre",
}

_HS = r"(?:HS|H\.S\.?|HORS SERVICE)"
_RED_SEVERE = re.compile(
    r"(?<![A-Z])(VGE|VEI|EPAVE|POUR PIECES|HORS D.?USAGE|SANS (?:CARTE GRISE|CG)|CG NON DISPONIBLE"
    rf"|MOTEUR {_HS}|CASSE MOTEUR|MOTEUR CASSE|BOITE(?: DE VITESSES?| AUTO(?:MATIQUE)?)? {_HS}"
    rf"|BV[AM]? {_HS}|NON ROULANT|NE DEMARRE PAS)(?![A-Z])"
)
_YELLOW_TEXT = re.compile(
    r"(?<![A-Z])(KILOMETRAGE NON (?:GARANTI|CERTIFIE)S?|KM NON GARANTIS?|COMPTEUR CHANGE)(?![A-Z])"
)
_MINOR_TEXT = {
    "VOYANT": (3, "Voyant allumé signalé"),
    "BRUIT": (3, "Bruit signalé"),
    "FUMEE": (5, "Fumée signalée"),
    "CHOC": (3, "Choc signalé"),
    "GRELE": (5, "Grêle signalée"),
    "RAYURE": (1, "Rayures signalées"),
}
_NEGATORS = {"NON", "PAS", "SANS", "AUCUN", "AUCUNE", "NI", "JAMAIS"}
_PREFIX_TOKENS = {"CLASSE", "SERIE", "GRAND", "RANGE", "NEW", "MODEL", "NOUVELLE", "NOUVEAU", "ID.", "E"}
# A negation only reaches words of its own clause: "Km non garanti, moteur HS" is a broken
# engine, and "Pas de voyant, bruit train avant" still reports a noise.
_CLAUSE_SPLIT = re.compile(r"[.,;:!?/()\[\]\n]|\s-\s|\bMAIS\b")
_IMMEDIATE_NEG = re.compile(r"\b(?:PAS|SANS|AUCUNE?|NI|JAMAIS|NON)\b(?:\s+(?:DE|DU|DES|D'))?\s*$")


def _clause_before(text: str, start: int) -> str:
    """The part of the clause that precedes ``start`` (at most 30 characters back)."""
    return _CLAUSE_SPLIT.split(text[max(0, start - 30):start])[-1]


def _negated(text: str, start: int) -> bool:
    """True when one of the 3 words before ``start``, in the same clause, is a negation
    ('pas de fumée', 'aucun voyant allumé')."""
    return any(tok in _NEGATORS for tok in re.findall(r"[A-Z]+", _clause_before(text, start))[-3:])


def _negated_strict(text: str, start: int) -> bool:
    """For red deal-breakers: only a negation right before the words counts ('non VGE',
    'pas de moteur HS'). Missing a broken engine costs more than a false red, which a person
    reviews anyway."""
    return bool(_IMMEDIATE_NEG.search(_clause_before(text, start)))


def _first_affirmed(rx: re.Pattern[str], text: str,
                    negated: Callable[[str, int], bool] = _negated) -> Optional[re.Match[str]]:
    for m in rx.finditer(text):
        if not negated(text, m.start()):
            return m
    return None


def add_months(day: date, months: int) -> date:
    month = day.month - 1 + months
    year, month = day.year + month // 12, month % 12 + 1
    last = [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
            31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]
    return date(year, month, min(day.day, last))


_LEASED_RE = re.compile(r"BATTERIE\s+(?:EN\s+)?LOCATION|LOCATION\s+DE\s+(?:LA\s+)?BATTERIE")

# Body damage ("Défauts esthétiques"): a small, capped condition penalty, never a gate (the
# list is cosmetic; structure is gated through the CT). Calibrated on two real pages only.
DAMAGE_FREE_POINTS = 3  # a few touch-ups are normal at auction
DAMAGE_MAX_PENALTY = 6  # condition points: at most 2.1 points of the final score
_WHEEL_ZONE = re.compile(r"\b(?:JANTES?|ENJOLIVEURS?|PNEUS?|ROUES?)\b")
_HEAVY_KIND = re.compile(r"DEBOSSEL|BOSSE|ENFONC|REMPLAC|CHANG|CASS|FISSUR|DECHIR|PERFOR|ROUILL|CORRO|GRELE")

BATTERY_SOH_AGREE = 2.0  # points: listing and certificate agree (the listing shows floor(cert))
BATTERY_CERT_MIN_CONFIDENCE = 0.8


@dataclass
class Facts:
    vehicle_id: int
    house: str
    brand: Optional[str]
    model: Optional[str]
    trim: Optional[str]
    fuel: Optional[str]
    gearbox: Optional[str]
    year: Optional[int]
    first_registration: Optional[str]
    mileage_km: Optional[int]
    engine_cc: Optional[int]
    battery_soh_pct: Optional[float]
    battery_leased: Optional[int]
    warranty: Optional[str]
    comments: Optional[str]
    vat_regime: str
    price: Optional[float]
    sale_kind: str
    sale_date: Optional[date]
    ct: Optional[dict[str, Any]]  # parsed CT, or None
    ct_doc: Optional[str]  # None | 'parsed' | 'unreadable' | 'missing'
    # Added in h1-2026.11, with defaults so existing callers keep working.
    damages: Optional[list[dict[str, str]]] = None  # [{"zone", "kind"}]; None = not read, [] = none
    battery_cert: Optional[dict[str, Any]] = None  # whitelisted certificate reading (confidence >= 0.8)
    battery_soh_source: Optional[str] = None  # "listing" | "certificate" | "lowest" | None
    price_basis: Optional[str] = None  # which listing value ``price`` is: "start" (mise à prix) | "bid"


@dataclass
class Gate:
    code: str
    max_tier: str
    severe: bool
    text: str
    src: str
    ref: str


@dataclass
class Partial:
    facts: Facts
    R: float
    C: float
    gates: list[Gate] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    reasons: list[dict[str, Any]] = field(default_factory=list)
    effective_price: Optional[float] = None
    issues: list[str] = field(default_factory=list)  # reasons for 'partial' confidence
    engine_flags: list[str] = field(default_factory=list)
    # The effective price's parts, for the site's price table: {"start", "vat", "fees",
    # "repairs", "ct_redo", "total"}, rounded to the cent; they add up to ``total``.
    price_parts: Optional[dict[str, float]] = None

    @property
    def cohort_eligible(self) -> bool:
        ct = self.facts.ct or {}
        return (
            self.effective_price is not None
            and not self.gates
            and ct.get("result") not in (RESULT_MAJOR, RESULT_CRITICAL)
        )


@lru_cache(maxsize=1)
def load_engine_kb() -> dict[str, Any]:
    path = Path(__file__).with_name("engines.json")
    return json.loads(path.read_text(encoding="utf-8"))


def pick_battery_soh(listing: Optional[float], cert: Optional[dict[str, Any]]) -> tuple[Optional[float], Optional[str]]:
    """(SoH, source) from the listing value and a battery-certificate reading.

    The listing wins when both agree within 2 points (it shows the certificate value
    rounded down); the certificate is used when the listing has none; when they disagree
    by more, the lower (more cautious) value is kept with source "lowest". A certificate
    read with a confidence below 0.8 is ignored.
    """
    c = None
    if cert and (cert.get("parse_confidence") or 0) >= BATTERY_CERT_MIN_CONFIDENCE:
        value = cert.get("soh_pct")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            c = float(value)
    if listing is None:
        return (c, "certificate") if c is not None else (None, None)
    if c is None or abs(listing - c) <= BATTERY_SOH_AGREE:
        return listing, "listing"
    return min(listing, c), "lowest"


def _warranty_text(label: str) -> str:
    """Reason text for a warranty. The bare card badge ("Garantie", a link to Alcopa's
    mechanical-warranty page) does not say which formula: "Garantie : Garantie" read oddly."""
    label = re.sub(r"\s+", " ", label).strip()
    folded = fold(label).strip()
    if folded == "GARANTIE":
        return "Garantie mécanique incluse (formule non précisée)"
    if folded.startswith("GARANTIE"):
        return label  # e.g. "Garantie Sérénité de 3 mois incluse"
    return f"Garantie : {label}"


def _damage_pairs(damages: Optional[list[Any]]) -> list[tuple[str, str]]:
    return [(d["zone"], d["kind"]) for d in damages or []
            if isinstance(d, dict) and isinstance(d.get("zone"), str) and isinstance(d.get("kind"), str)]


def damage_summary(damages: Optional[list[Any]]) -> list[dict[str, Any]]:
    """[{"zone", "kind", "n"}] in first-seen order, wheels included (for display and the AI)."""
    counts: dict[tuple[str, str], int] = {}
    for pair in _damage_pairs(damages):
        counts[pair] = counts.get(pair, 0) + 1
    return [{"zone": z, "kind": k, "n": n} for (z, k), n in counts.items()]


def _damage_penalty(damages: Optional[list[Any]]) -> tuple[int, Optional[str]]:
    """(condition points to remove, reason text) for the body-damage list.

    Points = distinct (zone, kind) pairs outside the wheels, dents and replacements counting
    double; the first ``DAMAGE_FREE_POINTS`` are free and the penalty is capped.
    """
    seen: set[tuple[str, str]] = set()
    zones: dict[str, str] = {}  # folded zone -> first label seen
    heavy_zones: set[str] = set()
    points = 0
    for zone, kind in _damage_pairs(damages):
        fz, fk = fold(zone).strip(), fold(kind).strip()
        if _WHEEL_ZONE.search(fz) or (fz, fk) in seen:
            continue
        seen.add((fz, fk))
        zones.setdefault(fz, zone)
        heavy = bool(_HEAVY_KIND.search(fk))
        points += 2 if heavy else 1
        if heavy:
            heavy_zones.add(fz)
    penalty = min(DAMAGE_MAX_PENALTY, max(0, points - DAMAGE_FREE_POINTS))
    if not penalty:
        return 0, None
    ordered = [z for z in zones if z in heavy_zones] + [z for z in zones if z not in heavy_zones]
    shown = ", ".join(zones[z].lower() for z in ordered[:3]) + ("…" if len(ordered) > 3 else "")
    n = len(zones)
    text = f"Défauts esthétiques sur {n} élément{'s' if n > 1 else ''} de carrosserie"
    if heavy_zones:
        text += f", dont {len(heavy_zones)} avec débosselage ou remplacement"
    return penalty, f"{text} ({shown})"


def _defect_count(n: int, kind: str) -> str:
    """French count of CT defects with real plurals: "sans défaillance", "1 défaillance mineure"."""
    if not n:
        return "sans défaillance"
    return f"{n} défaillance {kind}" if n == 1 else f"{n} défaillances {kind}s"


def _reason(pillar: str, code: str, delta: float, text: str, src: str, ref: str = "") -> dict[str, Any]:
    return {"code": code, "pillar": pillar, "delta": round(delta, 1), "text": text,
            "evidence": {"src": src, "ref": ref}}


def model_family(model: Optional[str]) -> Optional[str]:
    """'CLIO IV' → 'CLIO'; 'CLASSE A 180' → 'CLASSE A'; 'C-ELYSEE' stays one token."""
    if not model:
        return None
    tokens = model.strip().upper().split()
    if not tokens:
        return None
    if tokens[0] in _PREFIX_TOKENS and len(tokens) > 1:
        return f"{tokens[0]} {tokens[1]}"
    return tokens[0]


def registration_date(facts: Facts) -> Optional[date]:
    if facts.first_registration:
        try:
            return date.fromisoformat(facts.first_registration)
        except ValueError:
            pass
    if facts.year:
        return date(facts.year, 7, 1)
    return None


def match_engines(facts: Facts) -> list[dict[str, Any]]:
    text = fold(" ".join(filter(None, [facts.model, facts.trim])))
    out = []
    brand = fold(facts.brand).strip() if facts.brand else None
    for entry in load_engine_kb()["engines"]:
        m = entry["match"]
        if not brand or (m.get("brands") and brand not in m["brands"]):
            continue
        if facts.fuel and m.get("fuels") and facts.fuel not in m["fuels"]:
            continue
        cc_known_match = bool(facts.engine_cc and m.get("engine_cc") and facts.engine_cc in m["engine_cc"])
        if facts.engine_cc and m.get("engine_cc") and not cc_known_match:
            continue
        strong = re.search(m["text_regex"], text)
        # Weak patterns (e.g. "BLUEHDI 100", shared by 1.5 and 1.6 engines) need the cc to confirm.
        weak = m.get("weak_regex") and cc_known_match and re.search(m["weak_regex"], text)
        if not (strong or weak):
            continue
        if facts.year and not (m["year_from"] <= facts.year <= m["year_to"]):
            continue
        out.append(entry)
    return out


def _tighten_from_ct(p: Partial, ct: dict[str, Any]) -> None:
    """Gates from defects that are certain whatever the overall reading of the PV."""
    defects = ct.get("defects", [])
    critical = next((d for d in defects if d["severity"] == "critical"), None)
    if critical or ct.get("result") == RESULT_CRITICAL:
        if not any(g.code == "ct_critique" for g in p.gates):
            p.gates.append(Gate("ct_critique", "red", True, "CT défavorable pour défaillance critique", "ct",
                                critical["code"] if critical else "result"))
    safety = [d for d in defects if d["severity"] == "major" and d["system"] in CRITICAL_SYSTEMS]
    if safety and not any(g.code == "ct_major_safety" for g in p.gates):
        system = SYSTEM_FR.get(safety[0]["system"], safety[0]["system"]).lower()
        p.gates.append(Gate("ct_major_safety", "yellow", False,
                            f"Défaillance majeure : {system} ({safety[0]['code']})", "ct", safety[0]["code"]))


def assess(facts: Facts, settings: Settings, today: date, fee_model: Callable[..., float],
           fee_split: Optional[Callable[..., dict[str, float]]] = None) -> Partial:
    """Price-independent pillars, gates and the effective price.

    ``fee_split`` (the adapter's) only tells which part of ``fee_model``'s extra is VAT for
    the price table; without it the whole extra counts as fees. It never changes the price.
    """
    ref_date = (facts.sale_date or today) + timedelta(days=21)
    reg = registration_date(facts)
    age = (ref_date - reg).days / 365.25 if reg else None
    p = Partial(facts=facts, R=85.0, C=70.0)
    w_r, w_c = WEIGHTS["R"], WEIGHTS["C"]

    # ---------------------------------------------------------------- reliability
    if age is None:
        p.flags.append("age_unknown")
    elif age > 15:
        p.R -= 15
        p.reasons.append(_reason("R", "age", -15 * w_r, f"Âge élevé ({age:.0f} ans)", "listing", "first_registration"))
    elif age > 10:
        p.R -= 8
        p.reasons.append(_reason("R", "age", -8 * w_r, f"Plus de 10 ans ({age:.0f} ans)", "listing", "first_registration"))

    km = facts.mileage_km
    if km is None:
        p.flags.append("mileage_unknown")
        p.issues.append("mileage_unknown")
    else:
        band_pen = 25 if km > 250_000 else 15 if km > 200_000 else 8 if km > 150_000 else 0
        if band_pen:
            p.R -= band_pen
            p.reasons.append(_reason("R", "mileage", -band_pen * w_r, f"Kilométrage élevé ({km:,} km)".replace(",", " "), "listing", "mileage_km"))
        if age:
            per_year = km / max(age, 0.5)
            expected = 20_000 if facts.fuel == "diesel" else 15_000
            if per_year > expected * 1.33:
                pen = min(15.0, (per_year - expected * 1.33) / 1000)
                p.R -= pen
                p.reasons.append(_reason("R", "km_per_year", -pen * w_r, f"{per_year:,.0f}".replace(",", " ") + " km/an, au-dessus de la normale", "listing", "mileage_km"))
            elif facts.fuel == "diesel" and per_year < 7_000 and age > 3:
                p.R -= 4
                p.reasons.append(_reason("R", "diesel_short_trips", -4 * w_r, "Diesel peu roulé (risque FAP/EGR)", "listing", "mileage_km"))
            elif per_year < 10_000 and km < 150_000:
                p.reasons.append(_reason("R", "low_mileage", 2 * w_r, f"Kilométrage modéré ({per_year:,.0f} km/an)".replace(",", " "), "listing", "mileage_km"))
                p.R += 2

    for entry in match_engines(facts):
        p.R -= entry["penalty"]
        p.engine_flags.append(entry["id"])
        p.reasons.append(_reason("R", "engine_kb", -entry["penalty"] * w_r, entry["label_fr"], "kb", entry["id"]))
        if entry.get("catastrophic"):
            p.gates.append(Gate("engine_risk", settings.engine_risk_max_tier, False,
                                f"Motorisation à risque connu : {entry['label_fr']}", "kb", entry["id"]))

    # ---------------------------------------------------------------- condition
    needs_ct = age is None or age >= 4
    ct = facts.ct
    ct_redo = False
    absent = facts.ct_doc in (None, "missing") or (ct is None and facts.ct_doc != "unreadable")
    unreadable = not absent and (
        facts.ct_doc == "unreadable" or ct is None or ct.get("result") == "UNKNOWN"
        or ct.get("parse_confidence", 0) < 0.8 or ct.get("nature") == "volontaire"
    )
    if absent or (unreadable and not needs_ct):
        if needs_ct:
            p.C = 55
            p.flags.append("ct_absent")
            p.issues.append("ct_absent")
            p.reasons.append(_reason("C", "ct_absent", -15 * w_c, "Pas de contrôle technique disponible", "listing", "documents"))
        else:
            p.C = 85
            p.reasons.append(_reason("C", "ct_not_required", 15 * w_c, "Véhicule de moins de 4 ans : CT pas encore exigé", "listing", "first_registration"))
            if ct:  # a voluntary or uncertain CT on a young car can only tighten the result
                _tighten_from_ct(p, ct)
    elif unreadable:
        p.C = 55
        p.flags.append("ct_non_lu")
        p.issues.append("ct_unreadable")
        p.reasons.append(_reason("C", "ct_non_lu", -15 * w_c, "Contrôle technique illisible ou incertain", "ct", "parse_confidence"))
        p.gates.append(Gate("ct_non_lu", "yellow", False, "Contrôle technique illisible ou incertain", "ct", "parse_confidence"))
        if ct:
            _tighten_from_ct(p, ct)
    else:
        counts = ct.get("counts", {})
        defects = ct.get("defects", [])
        if ct["result"] == RESULT_FAVORABLE:
            pen = min(14, 2 * counts.get("minor", 0))
            p.C = 92 - pen
            p.reasons.append(_reason("C", "ct_favorable", (p.C - 70) * w_c,
                                     "CT favorable, " + _defect_count(counts.get("minor", 0), "mineure"), "ct", "result"))
        elif ct["result"] == RESULT_MAJOR:
            pen = sum(6 * (1.5 if d["system"] in CRITICAL_SYSTEMS else 1) for d in defects if d["severity"] == "major")
            pen += counts.get("minor", 0)
            p.C = max(15.0, 60 - pen)
            p.reasons.append(_reason("C", "ct_major", (p.C - 70) * w_c,
                                     "CT défavorable : " + _defect_count(counts.get("major", 0), "majeure"), "ct", "result"))
        elif ct["result"] == RESULT_CRITICAL:
            p.C = 10
            p.reasons.append(_reason("C", "ct_critique", (10 - 70) * w_c,
                                     "CT défavorable : " + _defect_count(counts.get("critical", 0), "critique"), "ct", "result"))
        _tighten_from_ct(p, ct)
        if ct.get("odometer_inconsistent"):
            p.gates.append(Gate("odometer", "yellow", False, "Historique kilométrique incohérent", "ct", "odometer"))
        # CT freshness for a private buyer: 6 months (2 months after an unfavourable CT).
        insp = ct.get("inspection_date")
        if settings.buyer_profile == "particulier" and needs_ct and insp:
            try:
                limit = add_months(date.fromisoformat(insp), 6 if ct["result"] == RESULT_FAVORABLE else 2)
            except ValueError:
                limit = None
            if limit is not None and ref_date >= limit:
                ct_redo = True
                p.C -= 5
                p.flags.append("ct_redo")
                p.reasons.append(_reason("C", "ct_redo", -5 * w_c, "CT à refaire pour une revente à un particulier", "ct", "inspection_date"))

    leased = bool(facts.battery_leased)
    folded_comments = fold(facts.comments or "")
    if _LEASED_RE.search(folded_comments):
        leased = True
    if leased:
        p.flags.append("battery_leased")
    soh, soh_source = facts.battery_soh_pct, facts.battery_soh_source
    if soh_source is None and facts.battery_cert:  # Facts built without service.load_facts
        soh, soh_source = pick_battery_soh(facts.battery_soh_pct, facts.battery_cert)
    if facts.fuel in ("electric", "plugin_hybrid") and soh is not None:
        # Shown rounded down (74.6 reads "74 %", like the listing); the thresholds are
        # integers, so the gate and the penalty are the same as with the listing value.
        src = "battery" if soh_source in ("certificate", "lowest") else "listing"
        shown = math.floor(soh)
        if soh < 75 and not leased:
            p.gates.append(Gate("battery_soh", "red", False, f"Batterie dégradée (SoH {shown} %)", src, "battery_soh_pct"))
        pen = 0 if soh >= 90 else 8 if soh >= 80 else 15
        if pen:
            p.C -= pen
            p.reasons.append(_reason("C", "battery_soh", -pen * w_c, f"État de santé batterie {shown} %", src, "battery_soh_pct"))
        if soh_source == "lowest":
            p.flags.append("battery_soh_mismatch")
            p.reasons.append(_reason("C", "battery_soh_mismatch", 0.0,
                                     f"SoH annoncé et SoH du certificat batterie différents : {shown} % retenu "
                                     "(valeur la plus basse)", "battery", "battery_cert"))

    if facts.warranty:
        p.C += 5
        p.reasons.append(_reason("C", "warranty", 5 * w_c, _warranty_text(facts.warranty), "listing", "warranty"))

    damage_pen, damage_text = _damage_penalty(facts.damages)
    if damage_pen:
        p.C -= damage_pen
        p.flags.append("body_damage")
        p.reasons.append(_reason("C", "body_damage", -damage_pen * w_c, damage_text, "listing", "damages"))

    if folded_comments:
        severe = _first_affirmed(_RED_SEVERE, folded_comments, _negated_strict)
        if severe:
            p.gates.append(Gate("comment_dealbreaker", "red", True,
                                f"Mention « {severe.group(1).lower()} » dans les commentaires", "comment", severe.group(1)))
        # Strict too: "Vendu sans garantie kilométrage non garanti" is a mileage warning.
        if _first_affirmed(_YELLOW_TEXT, folded_comments, _negated_strict):
            p.gates.append(Gate("mileage_not_guaranteed", "yellow", False, "Kilométrage non garanti", "comment", "kilometrage"))
            p.issues.append("mileage_not_guaranteed")
        minor_total = 0
        for word, (pen, text) in _MINOR_TEXT.items():
            hits = [m for m in re.finditer(rf"(?<![A-Z]){word}S?(?![A-Z])", folded_comments)
                    if not _negated(folded_comments, m.start())
                    and not folded_comments[max(0, m.start() - 5):m.start()] in ("PARE-", "PARE ")]
            if hits and minor_total + pen <= 10:
                minor_total += pen
                p.C -= pen
                p.reasons.append(_reason("C", f"comment_{word.lower()}", -pen * w_c, text, "comment", word.lower()))

    p.R = max(0.0, min(100.0, p.R))
    p.C = max(0.0, min(100.0, p.C))

    # ---------------------------------------------------------------- effective price
    if facts.price is not None and facts.price > 0:
        eff = fee_model(facts.price, facts.sale_kind, settings.buyer_profile, facts.vat_regime)
        repair = 0.0
        if ct:
            for d in ct.get("defects", []):
                if d["severity"] in ("major", "critical"):
                    cost = REPAIR_COST_MAJOR.get(d["system"], 150)
                    repair += cost * (2 if d["severity"] == "critical" else 1)
        if repair:
            p.flags.append("repair_estimate")
        redo = CT_REDO_COST if ct_redo else 0.0
        p.effective_price = round(eff + repair + redo, 2)
        vat = 0.0
        if fee_split is not None:
            vat = round(float(fee_split(facts.price, facts.sale_kind, settings.buyer_profile,
                                        facts.vat_regime).get("vat") or 0.0), 2)
        start = round(facts.price, 2)
        # The fees are what is left of the fee model's price, so the parts always add up.
        p.price_parts = {"start": start, "vat": vat, "fees": round(eff - start - vat, 2),
                         "repairs": round(repair, 2), "ct_redo": redo, "total": p.effective_price}
    else:
        p.issues.append("price_unknown")
        p.flags.append("price_unknown")
    return p


@dataclass
class Cohort:
    by_model: dict[tuple, list[tuple[int, int, float, int]]]
    by_brand: dict[tuple, list[tuple[int, int, float, int]]]


def build_cohort(partials: list[Partial]) -> Cohort:
    by_model: dict[tuple, list] = {}
    by_brand: dict[tuple, list] = {}
    for part in partials:
        f = part.facts
        if not part.cohort_eligible or not f.brand or not f.year:
            continue
        band = (f.mileage_km // 50_000) if f.mileage_km is not None else -1
        item = (f.year, band, part.effective_price, f.vehicle_id)
        by_model.setdefault((f.brand, model_family(f.model), f.fuel, f.gearbox), []).append(item)
        by_brand.setdefault((f.brand, f.fuel), []).append(item)
    return Cohort(by_model, by_brand)


def value_score(part: Partial, cohort: Cohort) -> tuple[float, str, Optional[float]]:
    """(V, basis, reference price). Neutral 50 with basis 'value_unknown' if no reference."""
    f = part.facts
    if part.effective_price is None:
        return 50.0, "value_unknown:price_unknown", None
    if not f.brand or not f.year:
        return 50.0, "value_unknown:no_identity", None
    band = (f.mileage_km // 50_000) if f.mileage_km is not None else None
    model_key = (f.brand, model_family(f.model), f.fuel, f.gearbox)
    steps = [
        ("model_y1_band", cohort.by_model.get(model_key, []), 1, 0, 5),
        ("model_y2_band1", cohort.by_model.get(model_key, []), 2, 1, 5),
        ("brand_fuel_y2", cohort.by_brand.get((f.brand, f.fuel), []), 2, None, 8),
    ]
    for name, items, dy, dband, min_n in steps:
        prices = [
            price for (y, b, price, vid) in items
            if vid != f.vehicle_id and abs(y - f.year) <= dy
            and (dband is None or band is None or b == -1 or abs(b - band) <= dband)
        ]
        if len(prices) >= min_n:
            ref = median(prices)
            v = max(0.0, min(100.0, 50 + 100 * (ref - part.effective_price) / ref))
            return v, f"{name}:n={len(prices)}", ref
    return 50.0, "value_unknown:few_comparables", None


def finalize(
    part: Partial, V: float, basis: str, ref_price: Optional[float], llm_adjustment: int, settings: Settings
) -> dict[str, Any]:
    reasons = list(part.reasons)
    if ref_price is not None:
        reasons.append(_reason("V", "value", (V - 50) * WEIGHTS["V"],
                               f"Prix estimé frais inclus {'sous' if V >= 50 else 'au-dessus de'} la médiane des véhicules comparables",
                               "cohort", basis))
    issues = list(part.issues)
    if basis.startswith("value_unknown") and "price_unknown" not in issues:
        issues.append("value_unknown")
    confidence = "partial" if issues else "full"

    base = WEIGHTS["R"] * part.R + WEIGHTS["C"] * part.C + WEIGHTS["V"] * V
    pre = base + llm_adjustment
    max_tier, cap, binding = "green", 100.0, None
    severe_cap = settings.yellow_min - 1 - 20
    for gate in part.gates:
        gate_cap = (severe_cap if gate.severe else settings.yellow_min - 1) if gate.max_tier == "red" else settings.green_min - 1
        if gate_cap < cap:
            cap, max_tier, binding = gate_cap, gate.max_tier, gate
    if confidence == "partial" and settings.green_min - 1 < cap:
        cap, max_tier = settings.green_min - 1, "yellow"
    final = int(round(max(0.0, min(pre, cap, 100.0))))
    for gate in part.gates:  # every gate is explained, binding or not
        delta = min(0.0, cap - pre) if gate is binding else 0.0
        reasons.append(_reason("gate", gate.code, delta, gate.text, gate.src, gate.ref))
    tier = "green" if final >= settings.green_min else "yellow" if final >= settings.yellow_min else "red"
    if TIER_ORDER[tier] > TIER_ORDER[max_tier]:  # safety net; caps already enforce this
        tier = max_tier
    reasons.sort(key=lambda r: abs(r["delta"]), reverse=True)
    return {
        "reliability": int(round(part.R)),
        "condition": int(round(part.C)),
        "value": int(round(V)),
        "base_score": int(round(base)),
        "llm_adjustment": int(llm_adjustment),
        "final_score": final,
        "tier": tier,
        "max_tier": max_tier,
        "confidence": confidence,
        "effective_price": part.effective_price,
        "price_parts": dict(part.price_parts) if part.price_parts else None,
        "value_basis": basis,
        "gates": [g.__dict__ for g in part.gates],
        "flags": sorted(set(part.flags + issues)),
        "reasons": reasons,
    }
