"""The weekly snapshot the web page shows: this week's sale, scored, plus proof of freshness.

The snapshot is the only data that leaves the owner's PC for the site. It is built from a
whitelist: specs, scores, the reasons behind them, the CT reading (result, defects), the
body-damage list (zone and kind only), the price breakdown, AI verdict and a link to the lot
on the auction site. Never VINs or their hashes, plates, comments, photos or internal ids.

Schema 2 (November 2026) only adds fields: every schema-1 field keeps its name and meaning
(except that ``reasons`` no longer repeats the gates, which ``gates`` lists), so a page
written for either schema renders both.
"""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime
from typing import Any, Optional

from .adapters._parsing import PARIS
from .adapters.alcopa import is_alcopa_url
from .config import Settings, parse_utc_iso, utc_iso, utc_now
from .privacy import scrub_for_llm
from .report import build_report_context, pick_rows
from .scoring.heuristics import SYSTEM_FR, WEIGHTS, damage_summary
from .scoring.llm import PROMPT_VERSION
from .scoring.service import PUBLIC_CARD_LIMIT, site_picks

SCHEMA_VERSION = 2
# PUBLIC_CARD_LIMIT (cars listed below the top picks; red cars are counted, not listed) now
# lives in scoring.service, next to the selection rule; it stays importable from here.
COMPACT_LIMIT = 800  # every other lot of the sale, one line each (a sale has about 570)
DEFECT_LIMIT = 40  # CT defects per sheet (24 at most on the 2026-11 sale)
DAMAGE_LIMIT = 40  # distinct (zone, kind) body-damage items per sheet
REASON_LIMIT = 15
MAX_SNAPSHOT_BYTES = 1_500_000  # the site's function refuses more than 2 MB
__all__ = ["COMPACT_LIMIT", "DEFECT_LIMIT", "MAX_SNAPSHOT_BYTES", "PUBLIC_CARD_LIMIT", "SCHEMA_VERSION",
           "build_snapshot", "sale_state"]

_CARD_FIELDS = ("title", "trim", "year", "mileage", "fuel", "gearbox", "tier", "tier_label", "score",
                "pillars", "confidence", "effective_price", "starting_price", "sale_date", "lot", "ct",
                "ct_counts", "verdict")

NATURE_FR = {"periodique": "périodique", "contre_visite": "contre-visite", "volontaire": "volontaire",
             "vehicule_ecole": "véhicule école", "other": "autre"}
PILLAR_FR = {"R": "Fiabilité", "C": "État", "V": "Prix", "llm": "IA"}
# Why a car is "incomplete" (capped below the favourable tier), in the order shown.
PARTIAL_FR = {
    "price_unknown": "mise à prix pas encore publiée",
    "value_unknown": "prix pas encore comparé (trop peu de voitures comparables)",
    "ct_absent": "pas de contrôle technique publié",
    "ct_unreadable": "contrôle technique illisible ou incertain",
    "mileage_unknown": "kilométrage inconnu",
    "mileage_not_guaranteed": "kilométrage non garanti",
}
_VALUE_UNKNOWN_FR = {
    "price_unknown": "mise à prix pas encore publiée",
    "few_comparables": "trop peu de voitures comparables",
    "no_identity": "marque ou année inconnue",
}
_SEVERITY_RANK = {"critical": 0, "major": 1, "minor": 2}
_CT_RESULTS = ("FAVORABLE", "DEFAVORABLE_MAJEURE", "DEFAVORABLE_CRITIQUE", "UNKNOWN")
_CT_DOCS = ("parsed", "unreadable", "missing")


def fees_note(settings: Settings) -> str:
    vat = "TVA si elle n'est pas récupérable, " if settings.buyer_profile == "particulier" else ""
    return ("Prix tout compris = mise à prix + frais acheteur (14,4 % TTC, minimum 360 € en salle) + "
            f"{vat}réparations estimées d'après le CT + CT à refaire le cas échéant. "
            "Frais fixes par lot non inclus.")


def _text(value: Any, limit: int) -> Optional[str]:
    """Published text from the auction site (defect labels, damage zones): VINs, plates,
    links, phones and e-mails removed again (defence in depth), one line, capped."""
    if not isinstance(value, str):
        return None
    out = " ".join((scrub_for_llm(value) or "").split())[:limit].strip()
    return out or None


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return round(float(value), 2)


def _price(raw: Any) -> Optional[dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    out = {k: _num(raw.get(k)) for k in ("start", "vat", "fees", "repairs", "ct_redo", "total")}
    if out["start"] is None or out["total"] is None:
        return None
    parts: dict[str, Any] = {k: (v if v is not None else 0.0) for k, v in out.items()}
    # "start" is the opening price, or the running bid when basis is "bid" (the table says so).
    parts["basis"] = "bid" if raw.get("basis") == "bid" else "start"
    return parts


def _value(card: dict[str, Any]) -> dict[str, Any]:
    basis = str(card.get("value_basis") or "")
    if basis.startswith("value_unknown") or ":n=" not in basis:
        why = _VALUE_UNKNOWN_FR.get(basis.partition(":")[2], "pas de référence")
        return {"known": False, "text": f"pas encore comparé : {why}"}
    try:
        n = int(basis.rsplit(":n=", 1)[1])
    except ValueError:
        n = None
    pct = int(card.get("value") or 50) - 50  # V = 50 + percent below the comparables' median
    median = f"la médiane de {n} voitures comparables" if n else "la médiane des voitures comparables"
    if pct > 0:
        text = f"{pct} % sous {median}"
    elif pct < 0:
        text = f"{-pct} % au-dessus de {median}"
    else:
        text = f"au niveau de {median}"
    return {"known": True, "text": text}


def _score(card: dict[str, Any]) -> dict[str, Any]:
    final = card.get("score") or 0
    before = (card.get("base_score") or 0) + (card.get("llm_adjustment") or 0)
    binding = next((r for r in card.get("all_reasons", []) if r["pillar"] == "gate" and r["delta"] < 0), None)
    cap = cap_gate = None
    if before > final:
        if binding is not None:
            cap, cap_gate = f"Plafonnée à {final} : {binding['text']}", binding["text"]
        elif card.get("confidence") == "partial":
            cap = f"Plafonnée à {final} tant que les données sont incomplètes"
    return {"before_cap": before, "llm": card.get("llm_adjustment") or 0, "cap": cap, "cap_gate": cap_gate}


def _defects(ct: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    items = [d for d in ct.get("defects") or [] if isinstance(d, dict) and d.get("severity") in _SEVERITY_RANK]
    items.sort(key=lambda d: (_SEVERITY_RANK[d["severity"]], str(d.get("code") or "")))
    out = [{"severity": d["severity"], "system": SYSTEM_FR.get(str(d.get("system")), "Autre"),
            "code": _text(d.get("code"), 20), "label": _text(d.get("label"), 200)}
           for d in items[:DEFECT_LIMIT]]
    return out, max(0, len(items) - DEFECT_LIMIT)


def _ct(card: dict[str, Any]) -> Optional[dict[str, Any]]:
    ct = card.get("ct_raw")
    if not isinstance(ct, dict):
        return None
    defects, more = _defects(ct)
    codes = {r.get("code") for r in card.get("all_reasons", [])}
    km = ct.get("inspection_km")
    return {
        "result": ct.get("result") if ct.get("result") in _CT_RESULTS else "UNKNOWN",
        "nature": NATURE_FR.get(str(ct.get("nature"))),
        "date": ct.get("inspection_date"),
        "km": km if isinstance(km, int) and not isinstance(km, bool) else None,
        "valid_until": ct.get("validity_date"),
        "odometer_inconsistent": bool(ct.get("odometer_inconsistent")),
        "redo": "ct_redo" in (card.get("flags") or []),
        "doubtful": "ct_non_lu" in codes,  # read, but the score does not trust it
        "defects": defects,
        "more": more,
    }


def _damages(raw: Any) -> Optional[list[dict[str, Any]]]:
    if not isinstance(raw, list):
        return None
    cleaned = []
    for item in raw:
        if isinstance(item, dict):
            zone, kind = _text(item.get("zone"), 60), _text(item.get("kind"), 60)
            if zone or kind:
                cleaned.append({"zone": zone or "Zone inconnue", "kind": kind or "Défaut"})
    return damage_summary(cleaned)[:DAMAGE_LIMIT]


def _battery(raw: Any) -> Optional[dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    soh = _num(raw.get("soh"))
    source = raw.get("source") if raw.get("source") in ("listing", "certificate", "lowest") else None
    return {"soh": math.floor(soh) if soh is not None else None, "source": source, "leased": bool(raw.get("leased"))}


def _detail(card: dict[str, Any]) -> dict[str, Any]:
    raw = card.get("detail_raw") or {}
    reasons = []
    for r in card.get("all_reasons", []):
        if r["pillar"] == "gate":
            continue  # gates are listed once, in "gates" (and in score.cap when one binds)
        text = r["text"] if r["pillar"] != "llm" else _text(r["text"], 300)
        reasons.append({"text": text, "src": r["src"], "pillar": PILLAR_FR.get(r["pillar"], r["pillar"]),
                        "pts": r["delta"]})
    reasons.sort(key=lambda r: -abs(r["pts"] or 0))
    flags = card.get("flags") or []
    incomplete = [text for flag, text in PARTIAL_FR.items() if flag in flags] \
        if card.get("confidence") == "partial" else []
    return {
        "score": _score(card),
        "value": _value(card),
        "incomplete": incomplete,
        "price": _price(raw.get("price")),
        "ct": _ct(card),
        "ct_doc": raw.get("ct_doc") if raw.get("ct_doc") in _CT_DOCS else None,
        "damages": _damages(raw.get("damages")),
        "battery": _battery(raw.get("battery")),
        "reasons": reasons[:REASON_LIMIT],
    }


def _url(card: dict[str, Any]) -> Optional[str]:
    url = card.get("url")
    return url if url and is_alcopa_url(url) else None


def _public_card(card: dict[str, Any]) -> dict[str, Any]:
    out = {k: card.get(k) for k in _CARD_FIELDS}
    out["url"] = _url(card)
    # Model output is scrubbed wherever it is published (links, phones, e-mails), as in _detail.
    out["verdict"] = _text(card.get("verdict"), 400)
    llm_texts = {r["text"] for r in card.get("all_reasons", []) if r.get("pillar") == "llm"}
    out["reasons"] = [{"text": _text(r["text"], 300) if r["text"] in llm_texts else r["text"], "src": r["src"]}
                      for r in card.get("reasons", [])][:3]
    out["gates"] = [g.get("text") for g in card.get("gates", []) if g.get("text")]
    # Schema 2.
    out["subtitle"] = card.get("subtitle")
    out["ct_result"] = card.get("ct_result")
    out["ct_line"] = card.get("ct_line")
    out["detail"] = _detail(card)
    return out


def _why(card: dict[str, Any]) -> Optional[str]:
    """The main reason a lot is not listed: its most negative point (a binding gate first)."""
    worst = sorted((r for r in card.get("all_reasons", []) if r["delta"] < 0), key=lambda r: r["delta"])
    return worst[0]["text"] if worst else None


def _compact(card: dict[str, Any]) -> dict[str, Any]:
    return {
        "lot": card.get("lot"), "title": card.get("title"), "year": card.get("year"),
        "mileage": card.get("mileage"), "fuel": card.get("fuel"), "gearbox": card.get("gearbox"),
        "effective_price": card.get("effective_price"), "score": card.get("score"), "tier": card.get("tier"),
        "ct_result": card.get("ct_result"), "why": _text(_why(card), 300), "url": _url(card),
    }


def _lot_key(card: dict[str, Any]) -> tuple:
    lot = card.get("lot")
    return (lot is None, lot if isinstance(lot, int) else 0, str(card.get("title") or ""))


def sale_state(sale: Optional[sqlite3.Row], now: datetime) -> str:
    """upcoming | today (sale day, before it starts) | in_progress | over | unknown."""
    if sale is None or not sale["starts_at"]:
        return "unknown"
    starts = parse_utc_iso(sale["starts_at"])
    ends = parse_utc_iso(sale["ends_at"]) if sale["ends_at"] else None
    local_now, local_start = now.astimezone(PARIS), starts.astimezone(PARIS)  # type: ignore[union-attr]
    if ends is not None and now > ends:
        return "over"
    if local_now.date() < local_start.date():
        return "upcoming"
    # Room sales start in the morning (about 09:30): from 08:00 on sale day it is under way.
    return "in_progress" if local_now.hour >= 8 else "today"


def _llm_models(conn: sqlite3.Connection, sale_id: int) -> list[str]:
    """The models behind this sale's verdicts under the current prompt version."""
    rows = conn.execute(
        """SELECT DISTINCT a.llm_model FROM llm_assessments a JOIN listings l ON l.vehicle_id = a.vehicle_id
           WHERE l.sale_id = ? AND a.status = 'ok' AND a.prompt_version = ? ORDER BY a.llm_model""",
        (sale_id, PROMPT_VERSION))
    return [r[0] for r in rows]


def encode(snapshot: dict[str, Any]) -> bytes:
    """The bytes ``publish`` uploads (compact JSON, UTF-8)."""
    return json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def build_snapshot(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    sale_id: Optional[int],
    crawl: Optional[dict[str, Any]] = None,
    llm: Optional[dict[str, Any]] = None,
    now: Optional[datetime] = None,
    public_limit: int = PUBLIC_CARD_LIMIT,
) -> dict[str, Any]:
    """The JSON the site shows. `crawl` and `llm` describe this run (status, counts, notes)."""
    now_dt = now or utc_now()
    sale = conn.execute("SELECT * FROM sales WHERE id = ?", (sale_id,)).fetchone() if sale_id is not None else None
    cards: list[dict[str, Any]] = []
    top: list[dict[str, Any]] = []
    others: list[dict[str, Any]] = []
    sale_cards: list[dict[str, Any]] = []
    provisional = False
    counts = {"green": 0, "yellow": 0, "red": 0}
    partial = 0
    if sale is not None:
        ctx = build_report_context(conn, settings, sale_id=sale_id, now=now_dt)
        sale_cards = ctx["cards"]
        by_id = {c["id"]: c for c in sale_cards}
        counts, partial = ctx["counts"], ctx["partial"]
        # The same selection the AI review planned for (scoring.service.site_picks): while
        # nothing is complete yet, the provisional top picks are shown.
        picks = ctx["picks"]
        if public_limit != PUBLIC_CARD_LIMIT:
            picks = site_picks(pick_rows(sale_cards), ctx["upcoming"], settings.top_n, public_limit)
        top = [by_id[vid] for vid in picks.top]
        cards = [by_id[vid] for vid in picks.rest]
        provisional = picks.provisional
        listed = set(picks.top) | set(picks.rest)
        # Every other lot of the sale, one line each (red cars and cars beyond the list).
        others = sorted((c for c in sale_cards if c["id"] not in listed), key=_lot_key)
    sale_info = None
    if sale is not None:
        starts = parse_utc_iso(sale["starts_at"]) if sale["starts_at"] else None
        sale_info = {
            "external_id": sale["external_id"],
            "title": sale["title"],
            "room": sale["room"],
            "date": starts.astimezone(PARIS).date().isoformat() if starts else None,
            "url": sale["url"] if is_alcopa_url(sale["url"]) else None,
            "state": sale_state(sale, now_dt),
        }
    llm_info = dict(llm or {})
    if sale is not None:
        llm_info["models_answered"] = _llm_models(conn, sale_id)  # type: ignore[arg-type]
    # Every listed car should carry an AI verdict; the gap is visible here.
    llm_info["coverage"] = {"listed": len(top) + len(cards),
                            "with_verdict": sum(1 for c in top + cards if c.get("verdict"))}
    compact = [_compact(c) for c in others[:COMPACT_LIMIT]]
    partial_why: dict[str, int] = {}
    for c in sale_cards:
        if c.get("confidence") == "partial":
            for flag in PARTIAL_FR:
                if flag in (c.get("flags") or []):
                    partial_why[flag] = partial_why.get(flag, 0) + 1
    out_counts: dict[str, Any] = {
        **counts, "partial": partial, "listed": len(top) + len(cards),
        "compact": {t: sum(1 for c in compact if c["tier"] == t) for t in ("green", "yellow", "red")},
        "partial_why": partial_why,
    }
    if len(others) > len(compact):
        out_counts["compact_dropped"] = len(others) - len(compact)
    snapshot = {
        "schema": SCHEMA_VERSION,
        "generated_at": utc_iso(now_dt),
        "sale": sale_info,
        "crawl": crawl or {},
        "llm": llm_info,
        "counts": out_counts,
        "provisional": provisional,
        "top": [_public_card(c) for c in top],
        "cards": [_public_card(c) for c in cards],
        "compact": compact,
        "scoring": {
            "weights": {PILLAR_FR[k]: int(round(v * 100)) for k, v in WEIGHTS.items()},
            "green_min": settings.green_min,
            "yellow_min": settings.yellow_min,
        },
        "fees_note": fees_note(settings),
    }
    if len(encode(snapshot)) > MAX_SNAPSHOT_BYTES and compact:
        # Too big for the site's function: the one-line list goes first (the sheets stay).
        snapshot["counts"]["compact_dropped"] = snapshot["counts"].get("compact_dropped", 0) + len(compact)
        snapshot["counts"]["compact"] = {t: 0 for t in ("green", "yellow", "red")}
        snapshot["compact"] = []
    return snapshot
