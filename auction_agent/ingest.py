"""Crawl one sale: listing → identity-aware upsert → detail and documents for new cars only.

Phase 1 runs are always *scoped* (one sale URL), so they never mark cars missing and
never purge; the dormancy/purge lifecycle is Phase 2 (docs/MASTER_PLAN.md §2).

Per-car behaviour:
- new, stub or previously failed car → detail page, then CT (and battery doc for EVs)
  downloaded into memory, parsed, redacted;
- known complete car, first seen in *this* sale (a relist) → one detail-page check: the
  CT is downloaded again only if its link changed (deliberate deviation (a));
- known complete car without a CT, whose detail page is older than 48 h → one re-check
  (catches CTs uploaded after the first listing);
- otherwise nothing: no detail page, no PDF, no LLM.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Callable, Optional

from . import db
from .adapters.base import AuctionAdapter, LotDetail, LotStub, ParseError
from .config import Settings, utc_iso, utc_now
from .documents import fetch_document
from .http_client import BlockedError, FetchError, RateLimitedError, RobotsDisallowedError
from .privacy import check_identity_key_ready, identity_hmac, redact

DOWNLOADED_TYPES = ("ct", "battery")
EV_FUELS = {"electric", "plugin_hybrid", "hybrid"}


@dataclass
class CrawlOptions:
    sale_url: str
    max_pages: Optional[int] = None
    limit: Optional[int] = None
    download_docs: bool = True
    force_unlock: bool = False


@dataclass
class CrawlReport:
    run_id: int
    status: str
    sale_id: Optional[int] = None
    reasons: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    failures: list[dict[str, str]] = field(default_factory=list)


def _sha(text: Optional[str]) -> Optional[str]:
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text else None


def _backoff(now_dt, attempts: int) -> str:
    return utc_iso(now_dt + timedelta(hours=min(24, 2 ** max(0, attempts))))


def run_crawl(
    settings: Settings,
    adapter: AuctionAdapter,
    options: CrawlOptions,
    *,
    now_fn: Callable = utc_now,
    log: Callable[[str], None] = lambda _msg: None,
) -> CrawlReport:
    retired = db.retire_legacy_database(settings.db_path)
    if retired:
        log(f"Old prototype database moved to {retired} (VIN/plate removed).")
    key, key_id = check_identity_key_ready(settings.identity_key_path)

    with db.open_db(settings.db_path) as conn:
        db.check_identity_key(conn, key_id)
        start = now_fn()
        scope = {"sale_url": options.sale_url, "max_pages": options.max_pages, "limit": options.limit,
                 "download_docs": options.download_docs, "scoped": True}
        run_id = db.acquire_run(
            conn, adapter.slug, "crawl", scope, utc_iso(start),
            stale_before=utc_iso(start - timedelta(minutes=15)), force=options.force_unlock,
        )
        report = CrawlReport(run_id=run_id, status="running")
        counts = {"lots_seen": 0, "expected_lots": 0, "new": 0, "relisted": 0, "detailed": 0,
                  "documents": 0, "failed": 0}
        try:
            _crawl(conn, settings, adapter, options, key, report, counts, now_fn, log)
        except (BlockedError, RateLimitedError, RobotsDisallowedError) as exc:
            report.status = "blocked"
            report.reasons.append(str(exc))
        except BaseException as exc:
            report.status = "failed"
            report.reasons.append(f"{exc.__class__.__name__}: {exc}")
            db.finish_run(conn, run_id, "failed", utc_iso(now_fn()), counts,
                          {"reasons": report.reasons}, error=repr(exc))
            raise
        report.counts = counts
        db.finish_run(conn, run_id, report.status, utc_iso(now_fn()), counts,
                      {"reasons": report.reasons, "failures": report.failures[:50]},
                      error="; ".join(report.reasons) if report.status != "ok" else None)
        return report


def _crawl(conn, settings, adapter, options, key, report, counts, now_fn, log) -> None:
    sale = adapter.sale_from_url(options.sale_url)
    log(f"Listing sale {sale.external_id} ({sale.url}) ...")
    listing = adapter.list_sale(sale, max_pages=options.max_pages)
    stubs = listing.stubs[: options.limit] if options.limit else listing.stubs
    limited = bool(options.limit and len(listing.stubs) > options.limit)
    if limited:
        listing.reasons.append(f"scoped:limit={options.limit}")
    report.reasons.extend(listing.reasons)
    counts["expected_lots"] = listing.sale.expected_lots or 0
    log(f"{len(listing.stubs)} lots listed ({'complete' if listing.complete else 'incomplete: ' + ', '.join(listing.reasons)}).")

    now = utc_iso(now_fn())
    with db.write_tx(conn):
        sale_id = db.upsert_sale(conn, listing.sale, now)
        snapshot = db.apply_snapshot(conn, adapter.slug, sale_id, stubs, now)
    report.sale_id = sale_id
    counts.update({k: snapshot[k] for k in ("lots_seen", "new", "relisted")})

    known = db.vehicles_for_external_ids(conn, adapter.slug, [s.external_id for s in stubs])
    first_listing_here = {
        row["external_id"]
        for row in conn.execute(
            """SELECT v.external_id FROM listings l JOIN vehicles v ON v.id = l.vehicle_id
               WHERE l.sale_id = ? AND l.first_seen_at = ?
                 AND EXISTS (SELECT 1 FROM listings o WHERE o.vehicle_id = v.id AND o.sale_id <> l.sale_id)""",
            (sale_id, now),
        )
    }

    for index, stub in enumerate(stubs, start=1):
        row = known[stub.external_id]
        action = _decide(row, stub.external_id in first_listing_here, settings, now_fn)
        if action is None:
            continue
        log(f"[{index}/{len(stubs)}] {stub.brand or ''} {stub.model or ''} ({stub.external_id}): {action}")
        try:
            _ingest_vehicle(conn, settings, adapter, stub, row, sale_id, action, options, key, counts, now_fn)
        except (BlockedError, RateLimitedError, RobotsDisallowedError):
            raise
        except (FetchError, ParseError, ValueError) as exc:
            counts["failed"] += 1
            report.failures.append({"external_id": stub.external_id, "error": str(exc)[:300]})
            now_dt = now_fn()
            with db.write_tx(conn):
                db.set_ingest_state(
                    conn, row["id"],
                    state="failed" if action == "ingest" else row["ingest_state"],
                    now=utc_iso(now_dt), error=str(exc)[:500],
                    next_attempt_at=_backoff(now_dt, row["ingest_attempts"] + 1) if action == "ingest" else None,
                )
        if index % 10 == 0:
            if not db.heartbeat(conn, report.run_id, utc_iso(now_fn())):
                raise RuntimeError("Run lock lost (another run took over); stopping.")

    report.status = "ok" if (listing.complete and not limited) else "incomplete"


def _decide(row, relisted_here: bool, settings: Settings, now_fn) -> Optional[str]:
    state = row["ingest_state"]
    if state == "stub":
        return "ingest"
    if state == "failed":
        if row["ingest_attempts"] >= settings.max_ingest_attempts:
            return None
        nxt = row["next_attempt_at"]
        return "ingest" if not nxt or nxt <= utc_iso(now_fn()) else None
    if relisted_here and settings.relist_detail_check:
        return "relist_check"
    if row["ct_state"] == "absent" and row["detail_fetched_at"]:
        cutoff = utc_iso(now_fn() - timedelta(hours=settings.late_ct_recheck_hours))
        if row["detail_fetched_at"] < cutoff:
            return "late_ct_check"
    return None


def _detail_fields(detail: LotDetail, stub: LotStub, key: bytes) -> dict[str, Any]:
    fields = {k: v for k, v in detail.fields.items() if k not in ("starting_price", "current_price_attr")}
    fields.setdefault("warranty", stub.warranty)
    if stub.battery_soh_pct is not None:
        fields.setdefault("battery_soh_pct", stub.battery_soh_pct)
    fields["vin_hmac"] = identity_hmac(key, detail.vin)
    comments = redact(detail.comments)
    fields["comments"] = comments
    fields["comments_sha256"] = _sha(comments)
    fields["raw_specs_json"] = json.dumps(detail.raw_specs, ensure_ascii=False) if detail.raw_specs else None
    fields["photo_count"] = detail.photo_count
    return fields


def _ingest_vehicle(conn, settings, adapter, stub, row, sale_id, action, options, key, counts, now_fn) -> None:
    vehicle_id = row["id"]
    detail = adapter.fetch_detail(stub)
    counts["detailed"] += 1
    now = utc_iso(now_fn())
    with db.write_tx(conn):
        db.update_vehicle_detail(conn, vehicle_id, _detail_fields(detail, stub, key))
        conn.execute(
            """UPDATE listings SET
                   starting_price = COALESCE(starting_price, :sp),
                   current_price_attr = COALESCE(:cp, current_price_attr)
               WHERE vehicle_id = :vid AND sale_id = :sid""",
            {"sp": detail.fields.get("starting_price"), "cp": detail.fields.get("current_price_attr"),
             "vid": vehicle_id, "sid": sale_id},
        )
        db.set_ingest_state(conn, vehicle_id, state=row["ingest_state"] if action != "ingest" else "stub",
                            now=now, detail_fetched=True)

    refs = {ref.doc_type: ref for ref in detail.documents}
    fuel = detail.fields.get("fuel") or stub.fuel
    ct_state = "present" if "ct" in refs else "absent"
    for doc_type, ref in refs.items():
        existing = db.get_document(conn, vehicle_id, doc_type)
        if doc_type not in DOWNLOADED_TYPES or (doc_type == "battery" and fuel not in EV_FUELS):
            if existing is None:
                with db.write_tx(conn):
                    db.upsert_document(conn, vehicle_id, {"doc_type": doc_type, "source_url": ref.url,
                                                          "fetch_status": "not_fetched"}, now)
            continue
        if not options.download_docs:
            continue
        if existing is not None and existing["fetch_status"] == "ok" and existing["source_url"] == ref.url:
            continue  # same document link as before: nothing to download
        doc = fetch_document(adapter.http, doc_type, ref.url, settings, referer=stub.detail_url)
        if existing is not None and doc.sha256 and doc.sha256 == existing["sha256"]:
            with db.write_tx(conn):
                db.touch_document_source(conn, vehicle_id, doc_type, ref.url)
            continue
        counts["documents"] += 1 if doc.fetch_status == "ok" else 0
        with db.write_tx(conn):
            db.upsert_document(conn, vehicle_id, doc.as_row(), utc_iso(now_fn()))
        if doc_type == "ct" and doc.fetch_status != "ok":
            ct_state = "failed"

    final_state = "complete" if (options.download_docs or row["ingest_state"] == "complete") else "stub"
    with db.write_tx(conn):
        db.set_ingest_state(conn, vehicle_id, state=final_state, now=utc_iso(now_fn()),
                            ct_state=ct_state if options.download_docs else None)
