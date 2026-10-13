"""Rebuild every active vehicle's assessment from current data.

The deterministic part is cheap and always recomputed. Only LLM output is cached
(``llm_assessments``), keyed by the hash of the exact payload sent.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from typing import Any, Callable, Optional

from .. import db
from ..adapters import get_adapter_class
from ..adapters._parsing import PARIS
from ..config import Settings, parse_utc_iso, utc_iso, utc_now
from .heuristics import (
    HEURISTIC_VERSION,
    Facts,
    Partial,
    assess,
    build_cohort,
    finalize,
    value_score,
)
from .llm import (
    PROMPT_VERSION,
    GeminiClient,
    LLMClient,
    LLMConfigError,
    LLMTransientError,
    payload_hash,
)


def _fee_models() -> Callable[[str], Callable[..., float]]:
    cache: dict[str, Callable[..., float]] = {}

    def get(house: str) -> Callable[..., float]:
        if house not in cache:
            try:
                cache[house] = get_adapter_class(house)(None).fee_model  # type: ignore[arg-type]
            except KeyError:
                cache[house] = lambda price, *_a: price
        return cache[house]

    return get


def _listing_end(row: sqlite3.Row) -> Optional[str]:
    return row["ends_at"] or row["closing_hint_at"] or row["starts_at"]


def best_listings(conn: sqlite3.Connection, now: str) -> dict[int, sqlite3.Row]:
    rows = conn.execute(
        """SELECT l.vehicle_id, l.sale_id, l.detail_url, l.lot_number, l.starting_price,
                  l.current_price_attr, l.closing_hint_at, l.last_seen_at,
                  s.kind, s.audience, s.room, s.title, s.starts_at, s.ends_at, s.status
           FROM listings l JOIN sales s ON s.id = l.sale_id"""
    ).fetchall()

    def rank(row: sqlite3.Row) -> tuple:
        # Live listings first, soonest end first (unknown end last); then most recently seen.
        end = _listing_end(row)
        live = row["status"] == "upcoming" and (end is None or end > now)
        if live:
            return (0, end or "9999", "")
        return (1, "", _invert(row["last_seen_at"]))

    best: dict[int, sqlite3.Row] = {}
    for row in rows:
        current = best.get(row["vehicle_id"])
        if current is None or rank(row) < rank(current):
            best[row["vehicle_id"]] = row
    return best


def _invert(ts: str) -> str:
    """Sort key that orders ISO timestamps newest-first."""
    return "".join(chr(0x7F - ord(c)) if c.isdigit() else c for c in ts)


def _price(row: Optional[sqlite3.Row]) -> Optional[float]:
    if row is None:
        return None
    values = [v for v in (row["starting_price"], row["current_price_attr"]) if v]
    return max(values) if values else None


def _ct_inputs(vehicle: sqlite3.Row, doc: Optional[sqlite3.Row]) -> tuple[Optional[dict], Optional[str]]:
    if doc is None:
        return None, ("missing" if vehicle["ct_state"] == "absent" else None)
    status = doc["fetch_status"]
    if status == "not_fetched":
        return None, None
    if status == "not_found":
        return None, "missing"
    if status == "ok" and doc["parse_status"] == "parsed" and doc["parsed_json"]:
        return json.loads(doc["parsed_json"]), "parsed"
    return None, "unreadable"


def load_facts(conn: sqlite3.Connection, now: str) -> list[Facts]:
    best = best_listings(conn, now)
    ct_docs = {r["vehicle_id"]: r for r in conn.execute("SELECT * FROM documents WHERE doc_type = 'ct'")}
    facts = []
    for v in conn.execute("SELECT * FROM vehicles WHERE lifecycle = 'active' ORDER BY id"):
        listing = best.get(v["id"])
        sale_date: Optional[date] = None
        if listing is not None and listing["starts_at"]:
            sale_date = parse_utc_iso(listing["starts_at"]).astimezone(PARIS).date()  # type: ignore[union-attr]
        ct, ct_doc = _ct_inputs(v, ct_docs.get(v["id"]))
        facts.append(Facts(
            vehicle_id=v["id"], house=v["house"], brand=v["brand"], model=v["model"], trim=v["trim"],
            fuel=v["fuel"], gearbox=v["gearbox"], year=v["year"], first_registration=v["first_registration"],
            mileage_km=v["mileage_km"], engine_cc=v["engine_cc"], battery_soh_pct=v["battery_soh_pct"],
            battery_leased=v["battery_leased"], warranty=v["warranty"], comments=v["comments"],
            vat_regime=v["vat_regime"], price=_price(listing),
            sale_kind=listing["kind"] if listing is not None else "room", sale_date=sale_date,
            ct=ct, ct_doc=ct_doc,
        ))
    return facts


def llm_payload(part: Partial) -> dict[str, Any]:
    f, ct = part.facts, part.facts.ct or {}
    return {
        "key": str(f.vehicle_id),
        "brand": f.brand, "model": f.model, "trim": f.trim, "fuel": f.fuel, "gearbox": f.gearbox,
        "year": f.year, "mileage_km": f.mileage_km, "engine_cc": f.engine_cc,
        "ct": {
            "result": ct.get("result"), "nature": ct.get("nature"), "counts": ct.get("counts"),
            "defects": [{k: d[k] for k in ("code", "severity", "label")} for d in ct.get("defects", [])][:15],
        } if ct else None,
        "engine_flags": part.engine_flags,
        "battery_soh_pct": f.battery_soh_pct,
        "warranty": bool(f.warranty),
        "comments": (f.comments or "")[:600] or None,
        "deterministic": {"reliability": round(part.R), "condition": round(part.C)},
    }


def rescore_all(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    today: Optional[date] = None,
    now: Optional[datetime] = None,
    use_llm: bool = True,
    llm_limit: Optional[int] = None,
    client: Optional[LLMClient] = None,
    log: Callable[[str], None] = lambda _m: None,
) -> dict[str, Any]:
    now_dt = now or utc_now()
    now_s = utc_iso(now_dt)
    today = today or now_dt.astimezone(PARIS).date()
    fee_for = _fee_models()
    notes: list[str] = []

    partials = [assess(f, settings, today, fee_for(f.house)) for f in load_facts(conn, now_s)]
    cohort = build_cohort(partials)
    values = {p.facts.vehicle_id: value_score(p, cohort) for p in partials}
    results = {p.facts.vehicle_id: finalize(p, *values[p.facts.vehicle_id], 0, settings) for p in partials}

    # ------------------------------------------------------------------ LLM triggers
    cache: dict[tuple[int, str], sqlite3.Row] = {}
    for row in conn.execute("SELECT * FROM llm_assessments WHERE status = 'ok' ORDER BY id"):
        cache[(row["vehicle_id"], row["llm_input_hash"])] = row
    model_name = (client.model if client is not None else settings.gemini_model) or "none"

    eligible = sorted(
        (p for p in partials if results[p.facts.vehicle_id]["tier"] != "red"
         and results[p.facts.vehicle_id]["confidence"] == "full"),
        key=lambda p: -results[p.facts.vehicle_id]["final_score"],
    )
    top_ids = {p.facts.vehicle_id for p in eligible[: settings.top_n]}
    triggered: list[tuple[Partial, str, dict[str, Any], str]] = []
    for part in partials:
        res = results[part.facts.vehicle_id]
        near = min(abs(res["base_score"] - settings.green_min), abs(res["base_score"] - settings.yellow_min)) <= 5
        kind = None
        if res["max_tier"] == "green" and res["confidence"] == "full" and near:
            kind = "adjust"
        elif res["tier"] == "green" or part.facts.vehicle_id in top_ids:
            kind = "verdict"
        if kind:
            payload = llm_payload(part)
            triggered.append((part, kind, payload, payload_hash(payload, model_name)))

    llm_calls = llm_cached = 0
    pending = [t for t in triggered if (t[0].facts.vehicle_id, t[3]) not in cache]
    llm_cached = len(triggered) - len(pending)
    if use_llm and pending:
        if client is None and settings.llm_enabled:
            try:
                client = GeminiClient(settings.gemini_api_key, settings.gemini_model)
            except LLMConfigError as exc:
                notes.append(str(exc))
        elif client is None:
            missing = [n for n, ok in (("GEMINI_API_KEY", settings.gemini_api_key), ("GEMINI_MODEL", settings.gemini_model),
                                       ("GEMINI_PAID_TIER=1", settings.gemini_paid_tier)) if not ok]
            notes.append(f"LLM review skipped for {len(pending)} vehicle(s): set {', '.join(missing)}.")
        if client is not None:
            budget = settings.llm_limit if llm_limit is None else llm_limit
            todo = pending[: max(0, budget)]
            if len(pending) > len(todo):
                notes.append(f"LLM budget reached: {len(pending) - len(todo)} vehicle(s) left for a later run.")
            for i in range(0, len(todo), settings.llm_batch_size):
                chunk = todo[i : i + settings.llm_batch_size]
                try:
                    batch = client.review([t[2] for t in chunk])
                except LLMConfigError as exc:
                    notes.append(str(exc))
                    break
                except LLMTransientError as exc:
                    notes.append(f"LLM temporarily unavailable: {exc}")
                    break
                llm_calls += 1
                with db.write_tx(conn):
                    for part, kind, payload, h in chunk:
                        review = batch.reviews.get(payload["key"])
                        if review is None:
                            continue
                        conn.execute(
                            """INSERT INTO llm_assessments (vehicle_id, kind, llm_input_hash, prompt_version,
                                   llm_model, adjustment, reasons_json, verdict, status, tokens_in, tokens_out, created_at)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ok', ?, ?, ?)""",
                            (part.facts.vehicle_id, kind, h, PROMPT_VERSION, client.model,
                             review.adjustment if kind == "adjust" else None,
                             json.dumps(review.reasons, ensure_ascii=False), review.verdict or None,
                             batch.tokens_in, batch.tokens_out, now_s),
                        )
                        cache[(part.facts.vehicle_id, h)] = conn.execute(
                            "SELECT * FROM llm_assessments WHERE id = last_insert_rowid()"
                        ).fetchone()

    # ------------------------------------------------------------------ final pass
    verdicts: dict[int, Optional[str]] = {}
    for part, kind, payload, h in triggered:
        row = cache.get((part.facts.vehicle_id, h))
        if row is None:
            continue
        vid = part.facts.vehicle_id
        adj = row["adjustment"] if (kind == "adjust" and row["adjustment"] is not None) else 0
        if adj:
            results[vid] = finalize(part, *values[vid], adj, settings)
            for text in json.loads(row["reasons_json"] or "[]"):
                results[vid]["reasons"].append({"code": "llm", "pillar": "llm", "delta": 0.0,
                                                "text": f"Avis IA : {text}",
                                                "evidence": {"src": "llm", "ref": str(row["id"])}})
        verdicts[vid] = row["verdict"]

    with db.write_tx(conn):
        conn.execute("DELETE FROM assessments")
        for part in partials:
            vid = part.facts.vehicle_id
            r = results[vid]
            conn.execute(
                """INSERT INTO assessments (vehicle_id, reliability, condition, value, base_score,
                       llm_adjustment, final_score, tier, max_tier, confidence, effective_price, value_basis,
                       gates_json, flags_json, reasons_json, verdict, heuristic_version, scored_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (vid, r["reliability"], r["condition"], r["value"], r["base_score"], r["llm_adjustment"],
                 r["final_score"], r["tier"], r["max_tier"], r["confidence"], r["effective_price"],
                 r["value_basis"], json.dumps(r["gates"], ensure_ascii=False),
                 json.dumps(r["flags"], ensure_ascii=False), json.dumps(r["reasons"], ensure_ascii=False),
                 verdicts.get(vid), HEURISTIC_VERSION, now_s),
            )
    tiers = [results[p.facts.vehicle_id]["tier"] for p in partials]
    return {
        "scored": len(partials),
        "green": tiers.count("green"),
        "yellow": tiers.count("yellow"),
        "red": tiers.count("red"),
        "llm_triggered": len(triggered),
        "llm_calls": llm_calls,
        "llm_cached": llm_cached,
        "notes": notes,
    }
