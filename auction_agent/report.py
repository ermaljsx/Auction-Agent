"""HTML report from persisted assessments (Jinja2, autoescaped, None-safe)."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from jinja2 import Environment, PackageLoader

from .adapters._parsing import PARIS
from .config import Settings, parse_utc_iso, utc_iso, utc_now
from .ct_parser import fold
from .scoring.service import best_listings, site_picks, upcoming_vehicle_ids

TIER_LABELS = {
    "green": "Profil favorable selon nos critères",
    "yellow": "Profil correct selon nos critères",
    "red": "Risque élevé selon nos critères",
}
FUEL_FR = {
    "petrol": "Essence", "diesel": "Diesel", "hybrid": "Hybride", "plugin_hybrid": "Hybride rechargeable",
    "electric": "Électrique", "lpg": "GPL", "cng": "GNV", "e85": "Éthanol E85", "other": "Autre",
}
GEARBOX_FR = {"manual": "Manuelle", "automatic": "Automatique"}
CT_FR = {
    "FAVORABLE": "CT favorable",
    "DEFAVORABLE_MAJEURE": "CT défavorable (défaillances majeures)",
    "DEFAVORABLE_CRITIQUE": "CT défavorable (défaillance critique)",
    "UNKNOWN": "CT illisible",
}
SRC_FR = {"ct": "contrôle technique", "listing": "annonce", "comment": "commentaires", "kb": "base moteurs",
          "cohort": "comparables", "llm": "IA", "battery": "certificat batterie"}


def fmt_int(value: Any, suffix: str = "") -> str:
    if value is None or value == "":
        return "—"
    try:
        return f"{int(round(float(value))):,}".replace(",", " ") + suffix
    except (TypeError, ValueError):
        return "—"


def fmt_eur(value: Any) -> str:
    text = fmt_int(value)
    return "prix inconnu" if text == "—" else f"{text} €"


def _env() -> Environment:
    env = Environment(loader=PackageLoader("auction_agent", "templates"), autoescape=True,
                      trim_blocks=True, lstrip_blocks=True)
    env.filters["int"] = fmt_int
    env.filters["eur"] = fmt_eur
    return env


def short_trim(model: Optional[str], trim: Optional[str]) -> Optional[str]:
    """The trim without the model words it repeats ("DS7 CROSSBACK" + "DS7 CROSSBACK PURETECH
    180" -> "PURETECH 180"). Accents and case are ignored when comparing. None when nothing is
    left (the trim only repeats the model) or there is no trim."""
    words = (trim or "").split()
    if not words:
        return None
    model_words = [fold(w).strip() for w in (model or "").split()]
    n = 0
    while n < len(words) and n < len(model_words) and fold(words[n]).strip() == model_words[n]:
        n += 1
    rest = " ".join(words[n:]).lstrip(" -–,")
    return rest or None


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _ct_summary(ct: dict[str, Any]) -> Optional[str]:
    """"favorable, 2 défaillances mineures", "défavorable, 6 défaillances majeures"; None when
    the result was not read."""
    counts = ct.get("counts") or {}
    result = ct.get("result")
    if result == "FAVORABLE":
        minor = int(counts.get("minor") or 0)
        return f"favorable, {_plural(minor, 'défaillance mineure', 'défaillances mineures')}" if minor \
            else "favorable, sans défaillance"
    if result in ("DEFAVORABLE_MAJEURE", "DEFAVORABLE_CRITIQUE"):
        serious = " et ".join(_plural(n, one, many) for n, one, many in (
            (int(counts.get("critical") or 0), "défaillance critique", "défaillances critiques"),
            (int(counts.get("major") or 0), "défaillance majeure", "défaillances majeures")) if n)
        return f"défavorable, {serious}" if serious else "défavorable"
    return None


def ct_line(ct: Optional[dict[str, Any]], reasons: list[dict[str, Any]]) -> str:
    """One line on the CT for a list row, consistent with how the score read it.

    A car under 4 years old needs no CT yet, but when one was published and parsed the line
    says what it found (BRIEF v2, B2): « pas encore exigé » alone contradicted a failed CT.
    """
    codes = {r.get("code") for r in reasons}
    if ct and codes & {"ct_young_failed", "ct_not_required"}:
        what = "CT volontaire" if ct.get("nature") == "volontaire" else "CT"
        summary = _ct_summary(ct)
        if summary is None:
            return f"{what} : résultat non lu (pas encore exigé)"
        return f"{what} {summary} (pas encore exigé)"
    if "ct_not_required" in codes:
        return "CT pas encore exigé (moins de 4 ans)"
    if "ct_absent" in codes:
        return "Pas de CT publié"
    if not ct:
        return "CT illisible" if "ct_non_lu" in codes else "Pas de CT publié"
    if "ct_non_lu" in codes:  # read, but not trusted by the score
        if ct.get("nature") == "volontaire":
            return "CT volontaire, pas retenu pour la note"
        if ct.get("result") in ("FAVORABLE", "DEFAVORABLE_MAJEURE", "DEFAVORABLE_CRITIQUE"):
            return "CT lu avec un doute, à vérifier sur le procès-verbal"
        return "CT illisible"
    counts = ct.get("counts") or {}
    result = ct.get("result")
    if result == "FAVORABLE":
        minor = int(counts.get("minor") or 0)
        return "CT favorable, sans défaillance" if not minor else             f"CT favorable, {_plural(minor, 'défaillance mineure', 'défaillances mineures')}"
    if result == "DEFAVORABLE_MAJEURE":
        major = int(counts.get("major") or 0)
        return f"CT défavorable, {_plural(major, 'défaillance majeure', 'défaillances majeures')}" if major             else "CT défavorable (défaillances majeures)"
    if result == "DEFAVORABLE_CRITIQUE":
        critical = int(counts.get("critical") or 0)
        return f"CT défavorable, {_plural(critical, 'défaillance critique', 'défaillances critiques')}" if critical             else "CT défavorable (défaillance critique)"
    return "CT illisible"


def _row_value(row: sqlite3.Row, key: str) -> Any:
    return row[key] if key in row.keys() else None


def _card(row: sqlite3.Row, listing: Optional[sqlite3.Row], ct_json: Optional[str]) -> dict[str, Any]:
    reasons = json.loads(row["reasons_json"])
    gates = json.loads(row["gates_json"])
    flags = json.loads(row["flags_json"])
    ct = json.loads(ct_json) if ct_json else None
    try:
        detail_raw = json.loads(_row_value(row, "detail_json") or "null")
    except ValueError:
        detail_raw = None
    all_reasons = [{"text": r["text"], "src": SRC_FR.get(r["evidence"]["src"], r["evidence"]["src"]),
                    "pillar": r["pillar"], "delta": r["delta"], "code": r.get("code"),
                    "ref": (r.get("evidence") or {}).get("ref"), "params": r.get("params")} for r in reasons]
    sale_date = None
    if listing is not None and listing["starts_at"]:
        sale_date = parse_utc_iso(listing["starts_at"]).astimezone(PARIS).strftime("%d/%m/%Y")  # type: ignore[union-attr]
    return {
        "id": row["vehicle_id"],
        "title": " ".join(filter(None, [row["brand"], row["model"]])) or "Véhicule",
        "trim": row["trim"],
        "year": row["year"],
        "mileage": row["mileage_km"],
        "fuel": FUEL_FR.get(row["fuel"] or "", row["fuel"]),
        "gearbox": GEARBOX_FR.get(row["gearbox"] or "", row["gearbox"]),
        "tier": row["tier"],
        "tier_label": TIER_LABELS[row["tier"]],
        "super_deal": row["tier"] == "green" and row["confidence"] == "full",
        "score": row["final_score"],
        "pillars": {"Fiabilité": row["reliability"], "État": row["condition"], "Prix": row["value"]},
        "confidence": row["confidence"],
        "flags": flags,
        "effective_price": row["effective_price"],
        "starting_price": listing["starting_price"] if listing is not None else None,
        "sale_kind": listing["kind"] if listing is not None else None,
        "sale_date": sale_date,
        "room": listing["room"] if listing is not None else None,
        "lot": listing["lot_number"] if listing is not None else None,
        "url": listing["detail_url"] if listing is not None else None,
        "ct": CT_FR.get(ct["result"], "CT") if ct else None,
        "ct_counts": ct.get("counts") if ct else None,
        # Gates are listed once, under "gates" (they used to be repeated here too).
        "reasons": [
            {"text": r["text"], "src": r["src"], "delta": r["delta"], "code": r["code"], "params": r["params"]}
            for r in all_reasons if r["pillar"] != "gate" and (r["delta"] != 0 or r["pillar"] == "llm")
        ][:3],
        "all_reasons": all_reasons,
        "gates": gates,
        "verdict": row["verdict"],
        "subtitle": short_trim(row["model"], row["trim"]),
        "base_score": row["base_score"],
        "llm_adjustment": row["llm_adjustment"],
        "value": row["value"],
        "value_basis": row["value_basis"],
        "detail_raw": detail_raw if isinstance(detail_raw, dict) else None,
        "ct_raw": ct,
        "ct_result": ct.get("result") if ct else None,
        "ct_line": ct_line(ct, reasons),
    }


def pick_rows(cards: list[dict[str, Any]]) -> list[tuple[int, dict[str, Any]]]:
    """Cards in the shape ``scoring.service.site_picks`` reads."""
    return [(c["id"], {"tier": c["tier"], "confidence": c["confidence"], "final_score": c["score"],
                       "effective_price": c["effective_price"]}) for c in cards]


def build_report_context(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    sale_id: Optional[int] = None,
    max_price: Optional[float] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Everything the report shows, as plain data (the HTML report and the web snapshot share it)."""
    now_dt = now or utc_now()
    listings = best_listings(conn, utc_iso(now_dt))
    upcoming = upcoming_vehicle_ids(conn, now_dt, settings)
    notes: list[str] = []
    if sale_id is not None and conn.execute("SELECT 1 FROM sales WHERE id = ?", (sale_id,)).fetchone() is None:
        # Users often pass the auction house's sale number: accept it too.
        row = conn.execute("SELECT id FROM sales WHERE external_id = ?", (str(sale_id),)).fetchone()
        if row is not None:
            sale_id = int(row["id"])
        else:
            notes.append(f"Aucune vente avec l'identifiant {sale_id} : utilisez `python -m auction_agent stats`.")
    query = """SELECT a.*, v.brand, v.model, v.trim, v.year, v.mileage_km, v.fuel, v.gearbox
               FROM assessments a JOIN vehicles v ON v.id = a.vehicle_id
               WHERE v.lifecycle = 'active'"""
    params: list[Any] = []
    if sale_id is not None:
        query += " AND EXISTS (SELECT 1 FROM listings l WHERE l.vehicle_id = v.id AND l.sale_id = ?)"
        params.append(sale_id)
    if max_price is not None:
        query += " AND (a.effective_price IS NULL OR a.effective_price <= ?)"
        params.append(max_price)
    query += " ORDER BY a.final_score DESC, a.effective_price ASC"
    # The same CT documents the score reads (scoring.service._ct_inputs).
    ct_docs = {r[0]: r[1] for r in conn.execute(
        """SELECT vehicle_id, parsed_json FROM documents WHERE doc_type = 'ct' AND fetch_status = 'ok'
           AND parse_status = 'parsed' AND parsed_json IS NOT NULL""")}
    cards = [_card(row, listings.get(row["vehicle_id"]), ct_docs.get(row["vehicle_id"]))
             for row in conn.execute(query, params)]

    # The site's selection rule (shared with the AI review and the snapshot). The report's
    # top picks are complete cars whose sale is still to come within the week only.
    picks = site_picks(pick_rows(cards), upcoming, settings.top_n)
    by_id = {c["id"]: c for c in cards}
    top = [] if picks.provisional else [by_id[vid] for vid in picks.top]
    if not cards:
        notes.append("Aucun véhicule ne correspond aux filtres.")
    top_ids = {c["id"] for c in top}
    sections = [
        (tier, TIER_LABELS[tier], [c for c in cards if c["tier"] == tier and c["id"] not in top_ids])
        for tier in ("green", "yellow", "red")
    ]
    return {
        "now": now_dt,
        "sale_id": sale_id,
        "cards": cards,
        "upcoming": upcoming,
        "picks": picks,
        "top": top,
        "sections": sections,
        "counts": {t: sum(1 for c in cards if c["tier"] == t) for t in ("green", "yellow", "red")},
        "partial": sum(1 for c in cards if c["confidence"] == "partial"),
        "max_price": max_price,
        "notes": notes,
    }


def generate_report(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    sale_id: Optional[int] = None,
    max_price: Optional[float] = None,
    output_path: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> Path:
    ctx = build_report_context(conn, settings, sale_id=sale_id, max_price=max_price, now=now)
    now_dt = ctx["now"]
    html = _env().get_template("report.html.j2").render(
        generated_at=now_dt.astimezone(PARIS).strftime("%d/%m/%Y %H:%M"),
        total=len(ctx["cards"]),
        counts=ctx["counts"],
        partial=ctx["partial"],
        top=ctx["top"],
        sections=ctx["sections"],
        max_price=max_price,
        buyer=settings.buyer_profile,
        notes=ctx["notes"],
    )
    if output_path is None:
        settings.reports_dir.mkdir(parents=True, exist_ok=True)
        output_path = settings.reports_dir / f"recommendation_{now_dt.astimezone(PARIS):%Y%m%d_%H%M%S}.html"
    output_path.write_text(html, encoding="utf-8")
    return output_path
