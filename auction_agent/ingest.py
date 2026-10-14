"""Crawl one sale: listing → identity-aware upsert → detail and documents for new cars only.

Phase 1 runs are always *scoped* (one sale URL), so they never mark cars missing and
never purge; the dormancy/purge lifecycle is Phase 2 (docs/MASTER_PLAN.md §2).

Per-car behaviour:
- new, stub or previously failed car → detail page, then CT (and battery doc for EVs)
  downloaded into memory, parsed, redacted;
- known complete car listed in a sale it had not been checked for yet (a relist) → one
  detail-page check: the CT is downloaded again only if its link changed (deviation (a));
  the check stays pending until it succeeds, so an interrupted run cannot lose it;
- known complete car without a CT, whose detail page is older than 48 h → one late
  re-check (catches CTs uploaded after the first listing), within a per-run budget;
  a CT whose download failed is retried the same way, at most 3 times;
- otherwise nothing: no detail page, no PDF, no LLM.

Failures are counted in the database (``ingest_attempts``) with an exponential backoff,
so a car that keeps failing is retried a bounded number of times and never blocks the
rest of the sale.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from . import db
from .adapters.base import AuctionAdapter, LotDetail, LotStub, ParseError
from .config import Settings, utc_iso, utc_now
from .documents import fetch_document, sweep_quarantine
from .http_client import BlockedError, FetchError, RateLimitedError, RobotsDisallowedError
from .privacy import check_identity_key_ready, identity_hmac, redact

DOWNLOADED_TYPES = ("ct", "battery")
EV_FUELS = {"electric", "plugin_hybrid", "hybrid"}
MAX_DOC_ATTEMPTS = 3
HEARTBEAT_EVERY = timedelta(seconds=60)


class RunLockLost(RuntimeError):
    pass


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


class _Heartbeat:
    """Refresh the run lock on elapsed time, and stop if another run took it over."""

    def __init__(self, conn, run_id: int, now_fn: Callable[[], datetime]):
        self.conn, self.run_id, self.now_fn = conn, run_id, now_fn
        self.last = now_fn()

    def beat(self, force: bool = False) -> None:
        now = self.now_fn()
        if force or now - self.last >= HEARTBEAT_EVERY:
            if not db.heartbeat(self.conn, self.run_id, utc_iso(now)):
                raise RunLockLost("Run lock lost (another run took over); stopping.")
            self.last = now


def run_crawl(
    settings: Settings,
    adapter: AuctionAdapter,
    options: CrawlOptions,
    *,
    now_fn: Callable[[], datetime] = utc_now,
    log: Callable[[str], None] = lambda _msg: None,
) -> CrawlReport:
    retired = db.retire_legacy_database(settings.db_path)
    if retired:
        log(f"Old prototype database moved to {retired} "
            "(VIN, plate, CT text, comments and photo links removed).")
    swept = sweep_quarantine(settings)
    if swept:
        log(f"Removed {swept} expired quarantined PDF(s).")
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
        except RunLockLost as exc:
            report.status = "failed"
            report.reasons.append(str(exc))
        except BaseException as exc:
            report.status = "failed"
            report.reasons.append(f"{exc.__class__.__name__}: {exc}")
            db.finish_run(conn, run_id, "failed", utc_iso(now_fn()), counts,
                          {"reasons": report.reasons}, error=repr(exc))
            raise
        report.counts = counts
        owned = db.finish_run(conn, run_id, report.status, utc_iso(now_fn()), counts,
                              {"reasons": report.reasons, "failures": report.failures[:50]},
                              error="; ".join(report.reasons) if report.status != "ok" else None)
        if not owned and report.status != "failed":
            report.status = "failed"
            report.reasons.append("run lock lost: another run took over while this one was running")
        return report


def _crawl(conn, settings, adapter, options, key, report, counts, now_fn, log) -> None:
    hb = _Heartbeat(conn, report.run_id, now_fn)
    sale = adapter.sale_from_url(options.sale_url)
    log(f"Listing sale {sale.external_id} ({sale.url}) ...")
    listing = adapter.list_sale(sale, max_pages=options.max_pages)
    hb.beat(force=True)
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
    # A relist check is pending until a detail page has been read after the car was first
    # seen in this sale, so a blocked or interrupted run cannot lose it.
    relist_pending = {
        row["external_id"]
        for row in conn.execute(
            """SELECT v.external_id FROM listings l JOIN vehicles v ON v.id = l.vehicle_id
               WHERE l.sale_id = ?
                 AND EXISTS (SELECT 1 FROM listings o WHERE o.vehicle_id = v.id AND o.sale_id <> l.sale_id)
                 AND (v.detail_fetched_at IS NULL OR v.detail_fetched_at < l.first_seen_at)""",
            (sale_id,),
        )
    }

    late_budget = settings.late_ct_budget
    doc_notes: set[str] = set()
    for index, stub in enumerate(stubs, start=1):
        hb.beat()
        row = known[stub.external_id]
        action = _decide(row, stub.external_id in relist_pending, settings, now_fn(), options.download_docs)
        if action == "late_ct_check":
            if late_budget <= 0:
                continue
            late_budget -= 1
        if action is None:
            continue
        log(f"[{index}/{len(stubs)}] {stub.brand or ''} {stub.model or ''} ({stub.external_id}): {action}")
        try:
            _ingest_vehicle(conn, settings, adapter, stub, row, sale_id, action, options, key, counts,
                            now_fn, doc_notes)
        except (BlockedError, RateLimitedError, RobotsDisallowedError) as exc:
            # Back this car off before stopping, so the next run does not start on it again.
            with db.write_tx(conn):
                db.record_failure(conn, row["id"], now_dt=now_fn(), error=str(exc),
                                  set_failed=row["ingest_state"] != "complete")
            raise
        except (FetchError, ParseError, ValueError, OSError) as exc:
            counts["failed"] += 1
            report.failures.append({"external_id": stub.external_id, "error": str(exc)[:300]})
            with db.write_tx(conn):
                db.record_failure(conn, row["id"], now_dt=now_fn(), error=str(exc),
                                  set_failed=row["ingest_state"] != "complete")
    report.reasons.extend(sorted(doc_notes))
    if late_budget <= 0 and settings.late_ct_budget > 0:
        report.reasons.append(f"late_ct_budget_reached={settings.late_ct_budget}")
    report.status = "ok" if (listing.complete and not limited) else "incomplete"


def _decide(row, relist_pending: bool, settings: Settings, now: datetime, download_docs: bool) -> Optional[str]:
    now_s = utc_iso(now)
    backoff_over = not row["next_attempt_at"] or row["next_attempt_at"] <= now_s
    exhausted = row["ingest_attempts"] >= settings.max_ingest_attempts
    state = row["ingest_state"]
    if state == "stub":
        if not download_docs and row["detail_fetched_at"]:
            return None  # --no-docs: the detail page was already read
        return "ingest" if backoff_over and not exhausted else None
    if state == "failed":
        return "ingest" if backoff_over and not exhausted else None
    # complete
    if not backoff_over or exhausted:
        return None
    if relist_pending and settings.relist_detail_check:
        return "relist_check"
    if not download_docs or not row["detail_fetched_at"]:
        return None
    cutoff = utc_iso(now - timedelta(hours=settings.late_ct_recheck_hours))
    if row["detail_fetched_at"] >= cutoff:
        return None
    if row["ct_state"] == "absent" and row["late_ct_checked_at"] is None:
        return "late_ct_check"
    if row["ct_state"] == "failed" and row["doc_attempts"] < MAX_DOC_ATTEMPTS:
        return "late_ct_check"
    return None


def _detail_fields(detail: LotDetail, stub: LotStub, key: bytes) -> dict[str, Any]:
    fields = {k: v for k, v in detail.fields.items() if k not in ("starting_price", "current_price_attr")}
    # A detail-page energy label can be coarser than the card's registration code
    # (e.g. "Essence" for a hybrid): never downgrade an electrified card value.
    if stub.fuel in EV_FUELS and fields.get("fuel") in ("petrol", "diesel"):
        fields["fuel"] = stub.fuel
    fields.setdefault("warranty", stub.warranty)
    if stub.battery_soh_pct is not None:
        fields.setdefault("battery_soh_pct", stub.battery_soh_pct)
    fields["vin_hmac"] = identity_hmac(key, detail.vin)
    comments = redact(detail.comments)
    fields["comments"] = comments
    fields["comments_sha256"] = _sha(comments)
    raw = {k: redact(v) for k, v in detail.raw_specs.items()}
    fields["raw_specs_json"] = json.dumps(raw, ensure_ascii=False) if raw else None
    fields["photo_count"] = detail.photo_count
    return fields


def _ingest_vehicle(conn, settings, adapter, stub, row, sale_id, action, options, key, counts, now_fn,
                    doc_notes: set[str]) -> None:
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
        db.mark_detail_fetched(conn, vehicle_id, now, late_ct_check=(action == "late_ct_check"))

    if not options.download_docs:
        if row["ingest_state"] == "complete":
            with db.write_tx(conn):
                db.mark_complete(conn, vehicle_id, ct_state=None)
        return  # a stub stays a stub until its documents are processed

    refs = {ref.doc_type: ref for ref in detail.documents}
    fuel = detail.fields.get("fuel") or stub.fuel
    if stub.fuel in EV_FUELS:
        fuel = stub.fuel
    ct_state = "present" if "ct" in refs else "absent"
    docs_ok = True
    for doc_type, ref in refs.items():
        existing = db.get_document(conn, vehicle_id, doc_type)
        if doc_type not in DOWNLOADED_TYPES or (doc_type == "battery" and fuel not in EV_FUELS):
            if existing is None:
                with db.write_tx(conn):
                    db.upsert_document(conn, vehicle_id, {"doc_type": doc_type, "source_url": ref.url,
                                                          "fetch_status": "not_fetched"}, now)
            continue
        existing_ok = existing is not None and existing["fetch_status"] == "ok"
        if existing_ok and existing["source_url"] == ref.url:
            continue  # same document link as before: nothing to download
        try:
            doc = fetch_document(adapter.http, doc_type, ref.url, settings, referer=stub.detail_url)
        except RobotsDisallowedError as exc:
            # Documents disallowed by robots.txt: keep going with the rest of the sale.
            doc_notes.add(f"documents_disallowed_by_robots: {exc}")
            if doc_type == "ct" and not existing_ok:
                ct_state, docs_ok = "failed", False
            continue
        if doc.fetch_status != "ok" or doc.parse_status in ("failed", "empty_text"):
            if existing_ok:
                doc_notes.add("kept_previous_document_after_failed_refresh")
                continue  # never replace a good document with a failed download
            docs_ok = False
        if existing is not None and doc.sha256 and doc.sha256 == existing["sha256"]:
            with db.write_tx(conn):
                db.touch_document_source(conn, vehicle_id, doc_type, ref.url)
            continue
        if doc.fetch_status == "ok":
            counts["documents"] += 1
        with db.write_tx(conn):
            db.upsert_document(conn, vehicle_id, doc.as_row(), utc_iso(now_fn()))
        if doc_type == "ct" and doc.fetch_status != "ok":
            ct_state = "failed"

    if ct_state == "absent" and (existing_ct := db.get_document(conn, vehicle_id, "ct")) is not None \
            and existing_ct["fetch_status"] == "ok":
        ct_state = "present"  # the link disappeared from the page but we hold a good copy
    with db.write_tx(conn):
        db.mark_complete(conn, vehicle_id, ct_state=ct_state, docs_ok=docs_ok)
