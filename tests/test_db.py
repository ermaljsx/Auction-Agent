import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import pytest

from auction_agent import db


@dataclass
class _Sale:
    house: str = "alcopa"
    external_id: str = "10206"
    kind: str = "room"
    audience: str = "public"
    room: Optional[str] = "Beauvais"
    title: Optional[str] = "Vente du 19 octobre 2026"
    url: str = "https://example.test/sale/10206"
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None
    expected_lots: Optional[int] = 2


@dataclass
class _Stub:
    external_id: str
    category: str = "voiture"
    lot_number: Optional[int] = None
    detail_url: str = "https://example.test/v"
    starting_price: Optional[float] = None
    current_price_attr: Optional[float] = None
    closing_hint_at: Optional[datetime] = None
    brand: Optional[str] = "PEUGEOT"
    model: Optional[str] = "208"
    trim: Optional[str] = None
    fuel: Optional[str] = "petrol"
    year: Optional[int] = 2019
    mileage_km: Optional[int] = 50000
    gearbox: Optional[str] = "manual"
    storage_location: Optional[str] = None


NOW = "2026-10-13T08:00:00Z"


def test_fresh_db_uses_wal_and_incremental_vacuum(settings):
    with db.open_db(settings.db_path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 2
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


def test_migrate_is_idempotent(settings):
    with db.open_db(settings.db_path):
        pass
    with db.open_db(settings.db_path) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"sales", "vehicles", "listings", "documents", "assessments", "sync_runs"} <= tables
    assert "photos" not in tables


def test_timestamp_format_is_enforced(settings):
    with db.open_db(settings.db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO sales (house, external_id, url, first_seen_at) VALUES ('h','1','u','2026-10-13 08:00:00')"
            )


def test_snapshot_apply_creates_listing_rows_and_keeps_detail_values(settings):
    with db.open_db(settings.db_path) as conn:
        with db.write_tx(conn):
            sale_id = db.upsert_sale(conn, _Sale(), NOW)
            counts = db.apply_snapshot(conn, "alcopa", sale_id, [_Stub("1"), _Stub("2")], NOW)
        assert counts == {"lots_seen": 2, "new": 2, "relisted": 0}
        assert conn.execute("SELECT count(*) FROM listings").fetchone()[0] == 2

        vid = conn.execute("SELECT id FROM vehicles WHERE external_id='1'").fetchone()[0]
        with db.write_tx(conn):
            db.update_vehicle_detail(conn, vid, {"mileage_km": 51234, "vin_hmac": "abc", "brand": None})
        # A listing-only refresh must not erase or overwrite detail values.
        with db.write_tx(conn):
            db.apply_snapshot(conn, "alcopa", sale_id, [_Stub("1", mileage_km=None, brand=None)], NOW)
        row = conn.execute("SELECT mileage_km, vin_hmac, brand FROM vehicles WHERE id=?", (vid,)).fetchone()
        assert (row["mileage_km"], row["vin_hmac"], row["brand"]) == (51234, "abc", "PEUGEOT")


def test_relist_into_new_sale_is_counted_and_history_kept(settings):
    with db.open_db(settings.db_path) as conn:
        with db.write_tx(conn):
            s1 = db.upsert_sale(conn, _Sale(external_id="1"), NOW)
            db.apply_snapshot(conn, "alcopa", s1, [_Stub("A", starting_price=1000)], NOW)
        with db.write_tx(conn):
            s2 = db.upsert_sale(conn, _Sale(external_id="2"), NOW)
            counts = db.apply_snapshot(conn, "alcopa", s2, [_Stub("A", starting_price=900)], NOW)
        assert counts["relisted"] == 1 and counts["new"] == 0
        prices = [r[0] for r in conn.execute("SELECT starting_price FROM listings ORDER BY sale_id")]
        assert prices == [1000, 900]


def test_cascade_delete_removes_children(settings):
    with db.open_db(settings.db_path) as conn:
        with db.write_tx(conn):
            sid = db.upsert_sale(conn, _Sale(), NOW)
            db.apply_snapshot(conn, "alcopa", sid, [_Stub("1")], NOW)
            vid = conn.execute("SELECT id FROM vehicles").fetchone()[0]
            db.upsert_document(conn, vid, {"doc_type": "ct", "source_url": "u", "fetch_status": "ok"}, NOW)
            conn.execute("DELETE FROM vehicles WHERE id=?", (vid,))
        for table in ("listings", "documents"):
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_run_lock_blocks_then_takes_over_stale(settings):
    with db.open_db(settings.db_path) as conn:
        run = db.acquire_run(conn, "alcopa", "crawl", {}, NOW, stale_before="2026-10-13T07:45:00Z")
        with pytest.raises(db.RunLockedError):
            db.acquire_run(conn, "alcopa", "crawl", {}, NOW, stale_before="2026-10-13T07:45:00Z")
        # 20 minutes later the heartbeat is stale: the next run takes over.
        later = "2026-10-13T08:20:00Z"
        run2 = db.acquire_run(conn, "alcopa", "crawl", {}, later, stale_before="2026-10-13T08:05:00Z")
        assert run2 != run
        assert conn.execute("SELECT status FROM sync_runs WHERE id=?", (run,)).fetchone()[0] == "failed"
        # The old run can no longer write (fenced).
        assert db.heartbeat(conn, run, later) is False


def test_legacy_v1_is_retired_without_identifiers(settings):
    settings.db_path.parent.mkdir(parents=True)
    with closing(sqlite3.connect(str(settings.db_path))) as legacy:
        legacy.execute("CREATE TABLE vehicles (id INTEGER PRIMARY KEY, detail_url TEXT, vin TEXT, registration TEXT)")
        legacy.execute("INSERT INTO vehicles VALUES (1, 'u', 'VF3ABCDEFGH123456', 'AB-123-CD')")
        legacy.commit()
    backup = db.retire_legacy_database(settings.db_path, datetime(2026, 10, 13, tzinfo=timezone.utc))
    assert backup is not None and backup.exists() and not settings.db_path.exists()
    with closing(sqlite3.connect(str(backup))) as conn:
        assert conn.execute("SELECT vin, registration FROM vehicles").fetchone() == (None, None)


def test_reclaim_space_shrinks_file(settings):
    with db.open_db(settings.db_path) as conn:
        with db.write_tx(conn):
            sid = db.upsert_sale(conn, _Sale(), NOW)
            db.apply_snapshot(conn, "alcopa", sid, [_Stub(str(i)) for i in range(400)], NOW)
            conn.execute("UPDATE vehicles SET comments = ?", ("x" * 2000,))
        pages_before = conn.execute("PRAGMA page_count").fetchone()[0]
        with db.write_tx(conn):
            conn.execute("DELETE FROM vehicles")
        db.reclaim_space(conn)
        assert conn.execute("PRAGMA freelist_count").fetchone()[0] == 0
        assert conn.execute("PRAGMA page_count").fetchone()[0] < pages_before
