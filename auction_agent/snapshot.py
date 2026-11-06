"""The weekly snapshot the web page shows: this week's sale, scored, plus proof of freshness.

The snapshot is the only data that leaves the owner's PC for the site. It is built from a
whitelist: specs, scores, the reasons behind them, CT summary, AI verdict and a link to the
lot on the auction site. Never VINs or their hashes, plates, comments, photos or internal ids.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any, Optional

from .adapters._parsing import PARIS
from .adapters.alcopa import is_alcopa_url
from .config import Settings, parse_utc_iso, utc_iso, utc_now
from .report import build_report_context
from .scoring.service import pick_key, pick_ids

SCHEMA_VERSION = 1
PUBLIC_CARD_LIMIT = 40  # cars listed below the top picks (red cars are counted, not listed)

_CARD_FIELDS = ("title", "trim", "year", "mileage", "fuel", "gearbox", "tier", "tier_label", "score",
                "pillars", "confidence", "effective_price", "starting_price", "sale_date", "lot", "ct",
                "ct_counts", "verdict")


def _public_card(card: dict[str, Any]) -> dict[str, Any]:
    out = {k: card.get(k) for k in _CARD_FIELDS}
    url = card.get("url")
    out["url"] = url if url and is_alcopa_url(url) else None
    out["reasons"] = [{"text": r["text"], "src": r["src"]} for r in card.get("reasons", [])][:3]
    out["gates"] = [g.get("text") for g in card.get("gates", []) if g.get("text")]
    return out


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
    rows = conn.execute(
        """SELECT DISTINCT a.llm_model FROM llm_assessments a JOIN listings l ON l.vehicle_id = a.vehicle_id
           WHERE l.sale_id = ? AND a.status = 'ok' ORDER BY a.llm_model""", (sale_id,))
    return [r[0] for r in rows]


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
    provisional = False
    counts = {"green": 0, "yellow": 0, "red": 0}
    partial = 0
    if sale is not None:
        ctx = build_report_context(conn, settings, sale_id=sale_id, now=now_dt)
        by_id = {c["id"]: c for c in ctx["cards"]}
        counts, partial = ctx["counts"], ctx["partial"]
        top = ctx["top"]
        if not top:  # nothing complete yet: the same provisional pick list the AI reviewed
            rows = ((c["id"], {"tier": c["tier"], "confidence": c["confidence"], "final_score": c["score"],
                               "effective_price": c["effective_price"]}) for c in ctx["cards"])
            top = [by_id[vid] for vid in pick_ids(rows, ctx["upcoming"], settings.top_n)]
            provisional = bool(top)
        top_ids = {c["id"] for c in top}
        rest = sorted((c for c in ctx["cards"] if c["id"] not in top_ids and c["tier"] != "red"),
                      key=lambda c: pick_key(c["score"], c["effective_price"]))
        cards = rest[:public_limit]
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
    return {
        "schema": SCHEMA_VERSION,
        "generated_at": utc_iso(now_dt),
        "sale": sale_info,
        "crawl": crawl or {},
        "llm": llm_info,
        "counts": {**counts, "partial": partial, "listed": len(top) + len(cards)},
        "provisional": provisional,
        "top": [_public_card(c) for c in top],
        "cards": [_public_card(c) for c in cards],
        "fees_note": ("Prix estimé = mise à prix + frais acheteur (14,4 % TTC, minimum 360 € en salle). "
                      "Frais fixes par lot non inclus."),
    }
