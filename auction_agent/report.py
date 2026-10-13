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
from .scoring.service import best_listings

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
          "cohort": "comparables", "llm": "IA"}


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


def _card(row: sqlite3.Row, listing: Optional[sqlite3.Row], ct_json: Optional[str]) -> dict[str, Any]:
    reasons = json.loads(row["reasons_json"])
    gates = json.loads(row["gates_json"])
    flags = json.loads(row["flags_json"])
    ct = json.loads(ct_json) if ct_json else None
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
        "reasons": [
            {"text": r["text"], "src": SRC_FR.get(r["evidence"]["src"], r["evidence"]["src"]), "delta": r["delta"]}
            for r in reasons if r["delta"] != 0 or r["pillar"] in ("gate", "llm")
        ][:3],
        "gates": gates,
        "verdict": row["verdict"],
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
    now_dt = now or utc_now()
    listings = best_listings(conn, utc_iso(now_dt))
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
    ct_docs = {r[0]: r[1] for r in conn.execute(
        "SELECT vehicle_id, parsed_json FROM documents WHERE doc_type = 'ct' AND parsed_json IS NOT NULL")}
    cards = [_card(row, listings.get(row["vehicle_id"]), ct_docs.get(row["vehicle_id"]))
             for row in conn.execute(query, params)]

    top = [c for c in cards if c["tier"] != "red" and c["confidence"] == "full"][: settings.top_n]
    top_ids = {c["id"] for c in top}
    sections = [
        (tier, TIER_LABELS[tier], [c for c in cards if c["tier"] == tier and c["id"] not in top_ids])
        for tier in ("green", "yellow", "red")
    ]
    html = _env().get_template("report.html.j2").render(
        generated_at=now_dt.astimezone(PARIS).strftime("%d/%m/%Y %H:%M"),
        total=len(cards),
        counts={t: sum(1 for c in cards if c["tier"] == t) for t in ("green", "yellow", "red")},
        partial=sum(1 for c in cards if c["confidence"] == "partial"),
        top=top,
        sections=sections,
        max_price=max_price,
        buyer=settings.buyer_profile,
    )
    if output_path is None:
        settings.reports_dir.mkdir(parents=True, exist_ok=True)
        output_path = settings.reports_dir / f"recommendation_{now_dt.astimezone(PARIS):%Y%m%d_%H%M%S}.html"
    output_path.write_text(html, encoding="utf-8")
    return output_path
