"""Rebuild every active vehicle's assessment from current data.

The deterministic part is cheap and always recomputed. Only LLM output is cached
(``llm_assessments``), keyed by the hash of the exact payload sent.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable, Iterable, Optional

from .. import db
from ..adapters import get_adapter_class
from ..adapters._parsing import PARIS
from ..config import Settings, parse_utc_iso, utc_iso, utc_now
from ..battery_parser import PARSER_VERSION as BATTERY_PARSER_VERSION
from ..ct_parser import PARSER_VERSION
from ..documents import reparse_stale_battery_documents, reparse_stale_ct_documents
from .heuristics import (
    BATTERY_CERT_MIN_CONFIDENCE,
    HEURISTIC_VERSION,
    Facts,
    Partial,
    assess,
    build_cohort,
    damage_summary,
    finalize,
    pick_battery_soh,
    value_score,
)
from ..privacy import scrub_for_llm
from .llm import (
    PROMPT_VERSION,
    LLMBatchError,
    LLMClient,
    LLMConfigError,
    LLMTransientError,
    make_client,
    payload_hash,
    payload_json,
)
from .pricing import cost_usd

# A payload the LLM failed on twice is not sent again until its data, the provider, the
# model, the reasoning effort or the prompt version changes (each changes the failure key).
MAX_LLM_FAILURES = 2
# Cars listed on the site below the top picks (red cars are counted, not listed).
PUBLIC_CARD_LIMIT = 40
# A batch is split at llm_batch_size cars or at this many payload characters (about 50K
# tokens), far below the 100K-token prompt size where Claude Haiku 5.5's dearer rate card starts.
MAX_BATCH_CHARS = 150_000
# What the AI sees of a car (see llm_payload).
MAX_DEFECTS = 40  # every CT defect in practice (24 at most on the 2026-11 sale)
MAX_DAMAGES = 30
MAX_COMMENT_CHARS = 1000
# What a price the estimate starts from can be (see _price_basis).
PRICE_BASES = ("start", "bid", "shown")


def pick_key(score: Any, price: Any) -> tuple:
    """Best score first; on a tie, the cheaper car first and unknown prices last."""
    return (-(score or 0), price is None, price or 0)


@dataclass(frozen=True)
class SitePicks:
    """The cars the site lists, in page order: the top picks, then the other cars."""

    top: list[int]
    rest: list[int]
    provisional: bool  # no car is complete yet: the top picks are the best incomplete ones

    @property
    def listed(self) -> list[int]:
        return self.top + self.rest


def site_picks(rows: Iterable[tuple[int, dict[str, Any]]], upcoming: set[int], top_n: int,
               limit: int = PUBLIC_CARD_LIMIT) -> SitePicks:
    """The one selection rule shared by the AI review, the HTML report and the web snapshot.

    ``rows`` are ``(vehicle_id, {"tier", "confidence", "final_score", "effective_price"})``.
    Red cars are never listed. The top picks are non-red cars of a sale in the next 7 days
    (``upcoming``) with complete data. While no car is complete yet (prices are often
    published late in the week), the best non-red upcoming cars are put forward
    provisionally instead, so the page is never empty and still gets AI verdicts. Then up to
    ``limit`` other non-red cars follow, best first. Ties are broken by ``pick_key`` and then
    the vehicle id, so every caller lists the same cars in the same order.
    """
    ranked = sorted(((vid, r) for vid, r in rows if r["tier"] != "red"),
                    key=lambda x: (*pick_key(x[1]["final_score"], x[1].get("effective_price")), x[0]))
    top = [vid for vid, r in ranked if vid in upcoming and r["confidence"] == "full"][: max(0, top_n)]
    provisional = False
    if not top:
        top = [vid for vid, _ in ranked if vid in upcoming][: max(0, top_n)]
        provisional = bool(top)
    chosen = set(top)
    rest = [vid for vid, _ in ranked if vid not in chosen][: max(0, limit)]
    return SitePicks(top, rest, provisional)


def pick_ids(rows: Iterable[tuple[int, dict[str, Any]]], upcoming: set[int], n: int) -> list[int]:
    """The top picks of ``site_picks`` (kept for callers that need only them)."""
    return site_picks(rows, upcoming, n, 0).top


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


def _fee_splits() -> Callable[[str], Callable[..., dict[str, float]]]:
    """Each house's ``fee_split`` (VAT and fees inside the effective price), cached."""
    cache: dict[str, Callable[..., dict[str, float]]] = {}

    def get(house: str) -> Callable[..., dict[str, float]]:
        if house not in cache:
            try:
                cache[house] = get_adapter_class(house)(None).fee_split  # type: ignore[arg-type]
            except KeyError:
                cache[house] = lambda price, *_a: {"vat": 0.0, "fees": 0.0}
        return cache[house]

    return get


def _detail(part: Partial, result: dict[str, Any]) -> dict[str, Any]:
    """Display data saved with the assessment (``assessments.detail_json``) for the site:
    the price breakdown, the CT document state, the body-damage list and the battery."""
    f = part.facts
    leased = "battery_leased" in result["flags"]
    battery = None
    if f.battery_soh_pct is not None or leased:
        battery = {"soh": f.battery_soh_pct, "source": f.battery_soh_source, "leased": leased}
    price = result.get("price_parts")
    if price is not None:  # "start" is the price the estimate starts from; "basis" says which one
        price = {**price, "basis": f.price_basis if f.price_basis in PRICE_BASES else "start"}
    return {
        "price": price,
        "ct_doc": f.ct_doc,
        "damages": getattr(f, "damages", None),  # [] = none reported, None = not read
        "battery": battery,
    }


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


def _price_basis(row: Optional[sqlite3.Row]) -> Optional[str]:
    """Which listing value ``_price`` is (see PRICE_BASES):

    - "start": the opening price (mise à prix);
    - "bid": a running bid, i.e. a current price above a known opening price;
    - "shown": a price shown without an opening price. ``data-current-price`` is the opening
      price until someone bids (adapters/alcopa.py), so it is most likely the opening price,
      but it cannot be told from a bid.
    """
    price = _price(row)
    if price is None:
        return None
    start = row["starting_price"]  # type: ignore[index]
    if not start:
        return "shown"
    return "bid" if price > start else "start"


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


# What scoring and the AI may see of a battery certificate: values only, no text.
_CERT_KEYS = ("soh_pct", "soh_source", "capacity_kwh", "capacity_new_kwh", "diagnostic_date",
              "diagnostic_km", "km_declared", "warranty_hint", "parse_confidence")


def _battery_inputs(doc: Optional[sqlite3.Row]) -> Optional[dict[str, Any]]:
    """The whitelisted reading of a battery certificate, or None (absent, unread, unsure)."""
    if doc is None or doc["fetch_status"] != "ok" or doc["parse_status"] != "parsed" or not doc["parsed_json"]:
        return None
    try:
        parsed = json.loads(doc["parsed_json"])
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    confidence = parsed.get("parse_confidence")
    soh = parsed.get("soh_pct")
    if not isinstance(confidence, (int, float)) or confidence < BATTERY_CERT_MIN_CONFIDENCE:
        return None
    if not isinstance(soh, (int, float)) or isinstance(soh, bool):
        return None
    return {k: parsed.get(k) for k in _CERT_KEYS}


def _damages_input(raw: Optional[str]) -> Optional[list[dict[str, str]]]:
    """The stored damage list as [{"zone", "kind"}] string pairs; None when not read or unreadable."""
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, list):
        return None
    return [{"zone": d["zone"], "kind": d["kind"]} for d in data
            if isinstance(d, dict) and isinstance(d.get("zone"), str) and isinstance(d.get("kind"), str)]


def load_facts(conn: sqlite3.Connection, now: str) -> list[Facts]:
    best = best_listings(conn, now)
    ct_docs = {r["vehicle_id"]: r for r in conn.execute("SELECT * FROM documents WHERE doc_type = 'ct'")}
    battery_docs = {r["vehicle_id"]: r for r in conn.execute(
        "SELECT vehicle_id, fetch_status, parse_status, parsed_json FROM documents WHERE doc_type = 'battery'")}
    facts = []
    for v in conn.execute("SELECT * FROM vehicles WHERE lifecycle = 'active' ORDER BY id"):
        listing = best.get(v["id"])
        sale_date: Optional[date] = None
        if listing is not None and listing["starts_at"]:
            sale_date = parse_utc_iso(listing["starts_at"]).astimezone(PARIS).date()  # type: ignore[union-attr]
        ct, ct_doc = _ct_inputs(v, ct_docs.get(v["id"]))
        cert = _battery_inputs(battery_docs.get(v["id"]))
        soh, soh_source = pick_battery_soh(v["battery_soh_pct"], cert)
        facts.append(Facts(
            vehicle_id=v["id"], house=v["house"], brand=v["brand"], model=v["model"], trim=v["trim"],
            fuel=v["fuel"], gearbox=v["gearbox"], year=v["year"], first_registration=v["first_registration"],
            mileage_km=v["mileage_km"], engine_cc=v["engine_cc"], battery_soh_pct=soh,
            battery_leased=v["battery_leased"], warranty=v["warranty"], comments=v["comments"],
            vat_regime=v["vat_regime"], price=_price(listing),
            sale_kind=listing["kind"] if listing is not None else "room", sale_date=sale_date,
            ct=ct, ct_doc=ct_doc,
            damages=_damages_input(v["damages_json"]), battery_cert=cert, battery_soh_source=soh_source,
            price_basis=_price_basis(listing),
        ))
    return facts


def upcoming_vehicle_ids(conn: sqlite3.Connection, now: datetime, settings: Settings,
                         horizon: timedelta = timedelta(days=7)) -> set[int]:
    """Cars with a live listing whose sale ends within ``horizon`` (the 'week')."""
    now_s, limit = utc_iso(now), utc_iso(now + horizon)
    out = set()
    for vid, row in best_listings(conn, now_s).items():
        end = _listing_end(row)
        live = row["status"] == "upcoming" and (end is None or end > now_s)
        audience_ok = row["audience"] == "public" or settings.buyer_profile == "pro"
        if live and audience_ok and (end is None or end <= limit):
            out.add(vid)
    return out


def _llm_text(value: Any, limit: int) -> str:
    """Untrusted text for the AI: redacted, links/amounts/phones/e-mails removed, one line, capped."""
    if value is None:
        return ""
    return " ".join((scrub_for_llm(str(value)) or "").split())[:limit]


def _is_empty(value: Any) -> bool:
    return value is None or (isinstance(value, (str, list, dict)) and not value)


def _compact(d: dict[str, Any], keep: tuple[str, ...] = ()) -> dict[str, Any]:
    """Drop None, "", [] and {} (an absent field means unknown); keep the named keys."""
    return {k: v for k, v in d.items() if k in keep or not _is_empty(v)}


_SEVERITY_RANK = {"critical": 0, "major": 1, "minor": 2}


def _ct_payload(ct: dict[str, Any]) -> dict[str, Any]:
    defects = [d for d in ct.get("defects") or [] if isinstance(d, dict)]
    defects.sort(key=lambda d: (_SEVERITY_RANK.get(d.get("severity"), 3), str(d.get("code") or "")))
    return _compact({
        "result": ct.get("result"),
        "nature": ct.get("nature"),
        # Fixed facts of the inspection: relists and the passing of time keep the hash.
        "date": ct.get("inspection_date"),
        "km": ct.get("inspection_km"),
        "counts": ct.get("counts"),
        "defects": [_compact({"code": d.get("code"), "severity": d.get("severity"), "system": d.get("system"),
                              "label": _llm_text(d.get("label"), 200)})
                    for d in defects[:MAX_DEFECTS]],
    })


def _damages_payload(damages: Optional[list[Any]]) -> list[dict[str, Any]]:
    """Body damage as sorted (zone, kind, n) items: the order on the page never changes the hash."""
    merged: dict[tuple[str, str], int] = {}
    for item in damage_summary(damages):
        zone, kind = _llm_text(item["zone"], 60), _llm_text(item["kind"], 60)
        if zone or kind:
            merged[(zone, kind)] = merged.get((zone, kind), 0) + int(item["n"])
    return [{"zone": z, "kind": k, "n": n} for (z, k), n in sorted(merged.items())][:MAX_DAMAGES]


def llm_payload(part: Partial) -> dict[str, Any]:
    """What the AI sees of one car. Never the price, the URL, the VIN, the plate or raw CT text.

    Comments are already redacted at ingest; amounts, phones, e-mails and links are also
    removed here. Date-dependent values (age, CT freshness, R/C) are left out so that relists
    and the passing of time do not change the payload hash (no re-paid call).
    """
    f, ct = part.facts, part.facts.ct or {}
    soh = f.battery_soh_pct
    return _compact({
        "key": str(f.vehicle_id),
        "brand": f.brand, "model": f.model, "trim": f.trim, "fuel": f.fuel, "gearbox": f.gearbox,
        "year": f.year, "mileage_km": f.mileage_km, "engine_cc": f.engine_cc,
        "engine_flags": list(part.engine_flags),
        "battery_soh_pct": round(float(soh), 1) if soh is not None else None,
        "warranty": bool(f.warranty),
        "ct": _ct_payload(ct) if ct else None,
        "damages": _damages_payload(getattr(f, "damages", None)),
        "comments": _llm_text(f.comments, MAX_COMMENT_CHARS) or None,
    }, keep=("key", "warranty"))


def _chunks(items: list[tuple], size: int, max_chars: int = MAX_BATCH_CHARS) -> list[list[tuple]]:
    """Split triggered items (payload at index 2) at ``size`` cars or ``max_chars`` characters."""
    out: list[list[tuple]] = []
    current: list[tuple] = []
    chars = 0
    for item in items:
        n = len(payload_json(item[2]))
        if current and (len(current) >= max(1, size) or chars + n > max_chars):
            out.append(current)
            current, chars = [], 0
        current.append(item)
        chars += n
    if current:
        out.append(current)
    return out


@dataclass
class ReviewPlan:
    """Scores before any AI adjustment, and the cars the AI should review, most useful first."""

    partials: list[Partial]
    values: dict[int, tuple]
    results: dict[int, dict[str, Any]]
    upcoming: set[int]
    pool: set[int]  # the cars of the sale being published (every active car without a sale)
    picks: SitePicks  # what the site lists, with a margin for AI adjustments
    triggered: list[tuple[Partial, str, dict[str, Any], str]]  # (part, kind, payload, cache hash)

    @property
    def listed_count(self) -> int:
        """Cars the site lists before AI adjustments (top picks plus PUBLIC_CARD_LIMIT)."""
        return len(self.picks.top) + min(len(self.picks.rest), PUBLIC_CARD_LIMIT)


def plan_reviews(conn: sqlite3.Connection, settings: Settings, *, now_dt: datetime, today: date,
                 model_name: str, sale_id: Optional[int] = None) -> ReviewPlan:
    """Score every active car with the rules and choose the AI reviews. Reads only.

    Triggers, only for cars whose sale ends within 7 days (no paid call for a sale that is
    over or further away):
    - ``adjust``: a green-capped car with complete data whose base score is within 5 points of
      a tier boundary (the AI adjustment may then change its tier);
    - ``verdict``: every car the site lists for the sale, and every other green car.
    The only way a car can join the list after the AI pass is that an adjusted car drops out
    of it, so both lists are taken with a margin of one car per ``adjust`` candidate: the
    other cars with one more car each, and the top picks with the next complete cars, which
    replace a dropped top pick even when they sit far below the other listed cars. The order
    is the page order (top picks, then the other listed cars), then those possible
    replacement top picks, then the other adjust cars, then the other green cars, so the run
    budget (LLM_LIMIT) goes first to what the site shows.
    """
    now_s = utc_iso(now_dt)
    fee_for, split_for = _fee_models(), _fee_splits()
    partials = [assess(f, settings, today, fee_for(f.house), fee_split=split_for(f.house))
                for f in load_facts(conn, now_s)]
    upcoming = upcoming_vehicle_ids(conn, now_dt, settings)
    cohort = build_cohort(partials)
    values = {p.facts.vehicle_id: value_score(p, cohort) for p in partials}
    results = {p.facts.vehicle_id: finalize(p, *values[p.facts.vehicle_id], 0, settings) for p in partials}
    active = [p.facts.vehicle_id for p in partials]
    if sale_id is None:
        pool = set(active)
    else:
        in_sale = {r[0] for r in conn.execute("SELECT vehicle_id FROM listings WHERE sale_id = ?", (sale_id,))}
        pool = {vid for vid in active if vid in in_sale}

    def near(res: dict[str, Any]) -> bool:
        return min(abs(res["base_score"] - settings.green_min), abs(res["base_score"] - settings.yellow_min)) <= 5

    adjust_ids = {vid for vid in active if vid in upcoming and results[vid]["max_tier"] == "green"
                  and results[vid]["confidence"] == "full" and near(results[vid])}
    pool_rows = [(vid, results[vid]) for vid in active if vid in pool]
    margin = len(adjust_ids & pool)
    picks = site_picks(pool_rows, upcoming, settings.top_n, PUBLIC_CARD_LIMIT + margin)
    # The complete cars next in line for the top picks (none while the top is provisional).
    wide = site_picks(pool_rows, upcoming, settings.top_n + margin, 0) if margin else None
    listed = set(picks.listed)
    reserve = [] if wide is None or wide.provisional else [vid for vid in wide.top if vid not in listed]
    rank = {vid: i for i, vid in enumerate(picks.listed + reserve)}

    triggered: list[tuple[Partial, str, dict[str, Any], str]] = []
    for part in partials:
        vid = part.facts.vehicle_id
        if vid not in upcoming:
            continue
        if vid in adjust_ids:
            kind = "adjust"
        elif results[vid]["tier"] == "green" or vid in rank:
            kind = "verdict"
        else:
            continue
        payload = llm_payload(part)
        triggered.append((part, kind, payload, payload_hash(payload, model_name)))

    def priority(t: tuple) -> tuple:
        vid = t[0].facts.vehicle_id
        if vid in rank:
            return (0, rank[vid], vid)
        return (1 if t[1] == "adjust" else 2, 0, vid)

    triggered.sort(key=priority)
    return ReviewPlan(partials, values, results, upcoming, pool, picks, triggered)


def _no_key_note(settings: Settings, n: int) -> str:
    note = (f"LLM review skipped for {n} vehicle(s): set {settings.llm_key_name} "
            "(copy .env.example to .env and paste your key).")
    hint = settings.llm_other_key_hint
    return f"{note} {hint}." if hint else note


def _reparse_with_note(conn: sqlite3.Connection, reparse: Callable[..., dict[str, int]], what: str,
                       version: str, notes: list[str], log: Callable[[str], None]) -> None:
    """Run one stale-document re-read and turn its counts into run notes. Never raises on
    a database error: the stored readings are then used as they are."""
    try:
        out = reparse(conn)
    except sqlite3.Error as exc:  # e.g. the database is busy: score with the stored parse
        notes.append(f"{what[0].upper()}{what[1:]}s not re-read this run ({exc.__class__.__name__}: {exc}).")
        return
    if out["reparsed"] or out.get("postponed"):
        msg = f"Re-read {out['reparsed']} {what}(s) with parser {version}."
        if out.get("postponed"):
            msg = msg[:-1] + f"; {out['postponed']} postponed (database busy)."
        notes.append(msg)
        log(msg)
    if out["failed"]:
        notes.append(f"{out['failed']} {what}(s) could not be re-read; their previous reading is kept.")


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
    sale_id: Optional[int] = None,
) -> dict[str, Any]:
    """Rebuild every assessment; run the AI review on the triggered cars (see plan_reviews).

    ``sale_id`` is the sale the site shows (``publish`` passes it): every car listed for it
    gets an AI verdict. Without it, the list is taken over every active car.
    """
    now_dt = now or utc_now()
    now_s = utc_iso(now_dt)
    today = today or now_dt.astimezone(PARIS).date()
    notes: list[str] = []

    # CT reports and battery certificates read by an older parser (or none yet) are re-read
    # from their stored (redacted) text first, so a parser fix reaches every car at the next
    # scoring run (MASTER_PLAN, ingest queue). The functions are looked up here, at call time.
    _reparse_with_note(conn, reparse_stale_ct_documents, "CT report", PARSER_VERSION, notes, log)
    _reparse_with_note(conn, reparse_stale_battery_documents, "battery certificate", BATTERY_PARSER_VERSION,
                       notes, log)

    model_name = (client.model if client is not None else settings.llm_model) or "none"
    plan = plan_reviews(conn, settings, now_dt=now_dt, today=today, model_name=model_name, sale_id=sale_id)
    partials, values, results, triggered = plan.partials, plan.values, plan.results, plan.triggered

    # ------------------------------------------------------------------ LLM cache and failures
    cache: dict[tuple[int, str], sqlite3.Row] = {}
    failures: dict[tuple[int, str], int] = {}
    if use_llm:  # --no-llm means heuristics only, cached LLM output included
        for row in conn.execute("SELECT * FROM llm_assessments WHERE status = 'ok' ORDER BY id"):
            cache[(row["vehicle_id"], row["llm_input_hash"])] = row
        for row in conn.execute(
            """SELECT vehicle_id, llm_input_hash, count(*) FROM llm_assessments
               WHERE status = 'failed' GROUP BY vehicle_id, llm_input_hash"""
        ):
            failures[(row[0], row[1])] = row[2]

    llm_calls = llm_cached = 0
    llm_error: Optional[str] = None  # "config" or "transient": why the LLM stopped this run
    pending = [t for t in triggered if (t[0].facts.vehicle_id, t[3]) not in cache]
    llm_cached = len(triggered) - len(pending)
    # Failures are counted per payload *and* request settings: changing the provider, the
    # model or the reasoning effort (the usual fix for a failing batch) gives every car a
    # fresh start.
    provider = getattr(client, "provider", None) or settings.llm_provider
    effort_tag = getattr(client, "reasoning_effort", None) or settings.llm_reasoning_effort
    fail_hash = {id(t): payload_hash(t[2], f"{provider}|{model_name}|{effort_tag}") for t in pending}

    def failed_before(t: tuple) -> int:
        return failures.get((t[0].facts.vehicle_id, fail_hash[id(t)]), 0)

    given_up = [t for t in pending if failed_before(t) >= MAX_LLM_FAILURES]
    if given_up:
        pending = [t for t in pending if t not in given_up]
        notes.append(f"LLM review skipped for {len(given_up)} vehicle(s) it already failed on twice "
                     "(retried when their data, the provider, the model, the reasoning effort or the prompt "
                     "changes).")

    tokens_in_total = tokens_out_total = 0
    cost_total = 0.0
    cost_known = True
    answered_models: list[str] = []
    cost_problems: list[str] = []
    built_here = False
    if use_llm and pending:
        if client is None and settings.llm_enabled:
            try:
                client = make_client(settings)
                built_here = True
            except LLMConfigError as exc:
                notes.append(str(exc))
                llm_error = "config"
        elif client is None:
            notes.append(_no_key_note(settings, len(pending)))
        if client is not None:
            budget = settings.llm_limit if llm_limit is None else llm_limit
            todo = pending[: max(0, budget)]
            if len(pending) > len(todo):
                notes.append(f"LLM budget reached: {len(pending) - len(todo)} vehicle(s) left for a later run.")
            # A car whose batch failed once is retried alone, so it cannot sink others again.
            retries = [t for t in todo if failed_before(t)]
            fresh = [t for t in todo if not failed_before(t)]
            chunks = [[t] for t in retries] + _chunks(fresh, settings.llm_batch_size)
            reasoning_tokens = missing = 0

            def account(model: str, tokens_in: Optional[int], tokens_out: Optional[int]) -> Optional[float]:
                """Add one billed call to the run totals; its cost, or None when unknown."""
                nonlocal tokens_in_total, tokens_out_total, cost_total, cost_known
                tokens_in_total += tokens_in or 0
                tokens_out_total += tokens_out or 0
                if model not in answered_models:
                    answered_models.append(model)
                cost = cost_usd(model, tokens_in, tokens_out)
                if cost is None:
                    cost_known = False
                    problem = (f"no price known for model {model}" if tokens_in is not None and tokens_out is not None
                               else "token usage not reported")
                    if problem not in cost_problems:
                        cost_problems.append(problem)
                else:
                    cost_total += cost
                return cost

            def record_failed(items: list, error: str, tokens: tuple = (None, None),
                              cost: Optional[float] = None) -> None:
                for i, t in enumerate(items):
                    conn.execute(
                        """INSERT INTO llm_assessments (vehicle_id, kind, llm_input_hash, prompt_version,
                               llm_model, status, error, tokens_in, tokens_out, cost_usd, created_at)
                           VALUES (?, ?, ?, ?, ?, 'failed', ?, ?, ?, ?, ?)""",
                        (t[0].facts.vehicle_id, t[1], fail_hash[id(t)], PROMPT_VERSION, client.model, error[:300],
                         tokens[0] if i == 0 else None, tokens[1] if i == 0 else None,
                         cost if i == 0 else None, now_s),
                    )

            for chunk in chunks:
                try:
                    batch = client.review([t[2] for t in chunk])
                except LLMConfigError as exc:
                    notes.append(str(exc))
                    llm_error = "config"
                    break
                except LLMBatchError as exc:
                    llm_calls += 1
                    reasoning_tokens += exc.tokens_reasoning or 0
                    cost = account(client.model, exc.tokens_in, exc.tokens_out)
                    with db.write_tx(conn):
                        record_failed(chunk, str(exc), (exc.tokens_in, exc.tokens_out), cost)
                    notes.append(f"LLM could not review {len(chunk)} vehicle(s): {exc}")
                    continue
                except LLMTransientError as exc:
                    notes.append(f"LLM temporarily unavailable: {exc}")
                    llm_error = "transient"
                    break
                except Exception as exc:  # never let the LLM prevent the heuristic rebuild
                    notes.append(f"LLM unavailable ({exc.__class__.__name__}): {exc}")
                    llm_error = "transient"
                    break
                llm_calls += 1
                reasoning_tokens += getattr(batch, "tokens_reasoning", None) or 0
                answered_by = getattr(batch, "model", None) or client.model
                cost = account(answered_by, batch.tokens_in, batch.tokens_out)
                first_row = True
                with db.write_tx(conn):
                    # A car left out of an otherwise valid answer counts as a failure too,
                    # so it is retried alone and eventually given up instead of resent forever.
                    left_out = [t for t in chunk if t[2]["key"] not in batch.reviews]
                    if len(left_out) == len(chunk):  # no row is 'ok': the batch totals go here
                        record_failed(left_out, "no review returned for this vehicle",
                                      (batch.tokens_in, batch.tokens_out), cost)
                    else:
                        record_failed(left_out, "no review returned for this vehicle")
                    missing += len(left_out)
                    for part, kind, payload, h in chunk:
                        review = batch.reviews.get(payload["key"])
                        if review is None:
                            continue
                        conn.execute(
                            """INSERT INTO llm_assessments (vehicle_id, kind, llm_input_hash, prompt_version,
                                   llm_model, adjustment, reasons_json, verdict, status, tokens_in, tokens_out,
                                   cost_usd, created_at)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ok', ?, ?, ?, ?)""",
                            # The adjustment is always stored, so a row cached as a 'verdict'
                            # still serves a later 'adjust' trigger for the same payload.
                            (part.facts.vehicle_id, kind, h, PROMPT_VERSION, answered_by,
                             review.adjustment, json.dumps(review.reasons, ensure_ascii=False),
                             review.verdict or None,
                             batch.tokens_in if first_row else None,  # batch totals, recorded once
                             batch.tokens_out if first_row else None,
                             cost if first_row else None, now_s),
                        )
                        first_row = False
                        cache[(part.facts.vehicle_id, h)] = conn.execute(
                            "SELECT * FROM llm_assessments WHERE id = last_insert_rowid()"
                        ).fetchone()
            if llm_calls:
                models = ", ".join(answered_models) or client.model
                line = (f"AI review: {llm_calls} call(s), {tokens_in_total:,} input + {tokens_out_total:,} "
                        "output tokens")
                if cost_known:
                    line += f", about ${cost_total:.4f} ({models})."
                else:
                    line += f"; {'; '.join(cost_problems)}: cost not computed ({models})."
                notes.append(line)
                log(line)
            if missing:
                notes.append(f"The model returned no review for {missing} vehicle(s); they are retried alone next run.")
            if reasoning_tokens:
                notes.append(f"The model used {reasoning_tokens} hidden reasoning tokens (billed as output, included "
                             f"above): LLM_REASONING_EFFORT={effort_tag} sets them, 'none' is the cheapest.")
            notes.extend(getattr(client, "notes", []) or [])
            if built_here:
                close = getattr(client, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:  # noqa: BLE001 - closing never fails the scoring run
                        pass

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
                       gates_json, flags_json, reasons_json, verdict, heuristic_version, scored_at, detail_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (vid, r["reliability"], r["condition"], r["value"], r["base_score"], r["llm_adjustment"],
                 r["final_score"], r["tier"], r["max_tier"], r["confidence"], r["effective_price"],
                 r["value_basis"], json.dumps(r["gates"], ensure_ascii=False),
                 json.dumps(r["flags"], ensure_ascii=False), json.dumps(r["reasons"], ensure_ascii=False),
                 verdicts.get(vid), HEURISTIC_VERSION, now_s,
                 json.dumps(_detail(part, r), ensure_ascii=False)),
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
        "llm_error": llm_error,
        "llm_provider": provider,
        "llm_model": ", ".join(answered_models) or model_name,
        "llm_tokens_in": tokens_in_total,
        "llm_tokens_out": tokens_out_total,
        "llm_cost_usd": round(cost_total, 6) if cost_known else None,
        "llm_listed": plan.listed_count,
        "notes": notes,
    }
