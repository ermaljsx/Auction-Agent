"""Regression tests for the Phase 1 review findings on storage and ingest."""
import sqlite3
from contextlib import closing
from datetime import timedelta

import pytest

from auction_agent import db
from auction_agent.http_client import BlockedError, FetchError, RobotsDisallowedError
from tests.test_ingest import IDS, SALE2, Clock, crawl, make_http


def _row(settings, external_id, cols="*"):
    with db.open_db(settings.db_path) as conn:
        return dict(conn.execute(f"SELECT {cols} FROM vehicles WHERE external_id = ?", (external_id,)).fetchone())


def _ct(settings, external_id):
    with db.open_db(settings.db_path) as conn:
        row = conn.execute(
            "SELECT d.* FROM documents d JOIN vehicles v ON v.id = d.vehicle_id "
            "WHERE v.external_id = ? AND d.doc_type = 'ct'", (external_id,)).fetchone()
        return dict(row) if row else None


def test_legacy_backup_holds_no_identifying_text(settings):
    """storage-1 / security-net-1: CT text, comments and photos are scrubbed too."""
    settings.db_path.parent.mkdir(parents=True)
    with closing(sqlite3.connect(str(settings.db_path))) as legacy:
        legacy.executescript("""
            CREATE TABLE vehicles (id INTEGER PRIMARY KEY, detail_url TEXT NOT NULL UNIQUE, vin TEXT,
                                   registration TEXT, comments TEXT, image_url TEXT);
            CREATE TABLE documents (id INTEGER PRIMARY KEY, vehicle_id INTEGER, extracted_text TEXT, local_path TEXT);
            CREATE TABLE photos (id INTEGER PRIMARY KEY, vehicle_id INTEGER, photo_url TEXT);
            INSERT INTO vehicles VALUES (1, 'u', 'VF3ABCDEFGH123456', 'AB-123-CD', 'plaque AB-123-CD', 'img');
            INSERT INTO documents VALUES (1, 1, 'Immatriculation : AB-123-CD VIN : VF3ABCDEFGH123456', 'x.pdf');
            INSERT INTO photos VALUES (1, 1, 'https://photos/1.jpg');
        """)
        legacy.commit()
    backup = db.retire_legacy_database(settings.db_path)
    raw = backup.read_bytes()
    assert b"VF3ABCDEFGH123456" not in raw and b"AB-123-CD" not in raw and b"photos/1.jpg" not in raw


def test_migration_from_v2_adds_columns(settings):
    settings.db_path.parent.mkdir(parents=True)
    with closing(sqlite3.connect(str(settings.db_path), isolation_level=None)) as conn:
        conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
        conn.execute("PRAGMA journal_mode = WAL")
        for statement in db._split_sql(db.SCHEMA_V2):
            conn.execute(statement)
        conn.execute("PRAGMA user_version = 2")
    with db.open_db(settings.db_path) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info('vehicles')")}
        assert {"late_ct_checked_at", "doc_attempts"} <= cols
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


def test_document_failures_are_bounded(settings):
    """storage-2: a CT that keeps failing is retried a bounded number of times."""
    http, clock = make_http(), Clock()
    http.routes[f"/getDocument/ct/CT{1098892}"] = FetchError("read timeout")
    for _ in range(12):
        crawl(settings, http, clock)
        clock.now += timedelta(hours=25)
    assert http.count(IDS["1098892"]) <= settings.max_ingest_attempts
    row = _row(settings, "1098892")
    assert row["ingest_attempts"] >= settings.max_ingest_attempts


def test_failed_refresh_keeps_previous_good_ct(settings):
    """storage-3."""
    clock = Clock()
    crawl(settings, make_http(), clock)
    before = _ct(settings, "1120464")
    http2 = make_http(sale_path=SALE2, ct_tokens={"1120464": "NEWTOKEN"})
    del http2.routes["/getDocument/ct/NEWTOKEN"]  # the new link 404s
    crawl(settings, http2, clock, sale=SALE2)
    after = _ct(settings, "1120464")
    assert after["fetch_status"] == "ok" and after["sha256"] == before["sha256"] and after["parsed_json"]
    assert _row(settings, "1120464")["ct_state"] == "present"


def test_relist_check_survives_an_interrupted_run(settings):
    """storage-4: a blocked run does not lose the pending relist checks."""
    clock = Clock()
    crawl(settings, make_http(), clock)
    http2 = make_http(sale_path=SALE2, ct_tokens={"1119509": "RENEWED"})
    http2.routes[IDS["1098892"]] = BlockedError("405")
    assert crawl(settings, http2, clock, sale=SALE2).status == "blocked"
    http3 = make_http(sale_path=SALE2, ct_tokens={"1119509": "RENEWED"})
    crawl(settings, http3, clock, sale=SALE2)
    assert http3.count("/getDocument/ct/RENEWED") == 1


def test_failed_ct_download_is_retried_later_and_capped(settings):
    """storage-5."""
    clock = Clock()
    http = make_http()
    http.routes["/getDocument/ct/CT1120464"] = "<html>Erreur temporaire</html>"
    crawl(settings, http, clock)
    assert _row(settings, "1120464")["ct_state"] == "failed"
    good = make_http()
    clock.now += timedelta(hours=49)
    crawl(settings, good, clock)
    assert _row(settings, "1120464")["ct_state"] == "present"
    # A permanently broken link stops after MAX_DOC_ATTEMPTS.
    bad = make_http()
    bad.routes["/getDocument/ct/CT1098892"] = "<html>Erreur</html>"
    for _ in range(6):
        clock.now += timedelta(hours=49)
        crawl(settings, bad, clock)
    assert bad.count("/getDocument/ct/CT1098892") <= 3


def test_documents_disallowed_by_robots_do_not_stop_the_run(settings):
    """storage-6."""
    http, clock = make_http(), Clock()
    for pid in IDS:
        http.routes[f"/getDocument/ct/CT{pid}"] = RobotsDisallowedError("robots.txt disallows /getDocument/")
    report = crawl(settings, http, clock)
    assert report.status == "ok" and report.counts["detailed"] == 3
    assert any("documents_disallowed_by_robots" in r for r in report.reasons)


def test_late_ct_check_happens_once(settings):
    """storage-8 / conformance-7."""
    clock = Clock()
    http = make_http(with_ct=False)
    crawl(settings, http, clock)
    for _ in range(4):
        clock.now += timedelta(hours=49)
        crawl(settings, http, clock)
    assert http.count(IDS["1120464"]) == 2  # first ingest + one late re-check


def test_late_ct_budget(settings):
    clock = Clock()
    crawl(settings, make_http(with_ct=False), clock)
    clock.now += timedelta(hours=49)
    http = make_http()
    report = crawl(settings.replace(late_ct_budget=1), http, clock)
    assert report.counts["detailed"] == 1
    assert any("late_ct_budget_reached" in r for r in report.reasons)


def test_no_docs_rerun_does_not_refetch_details(settings):
    """storage-9."""
    http, clock = make_http(), Clock()
    crawl(settings, http, clock, download_docs=False)
    crawl(settings, http, clock, download_docs=False)
    assert http.count("/voiture-occasion") == 3


def test_lost_lock_is_reported(settings):
    """storage-7: a run whose lock was taken over reports failure instead of 'ok'."""
    http, clock = make_http(), Clock()

    def takeover():
        with db.open_db(settings.db_path) as conn:
            conn.execute("UPDATE sync_runs SET status = 'failed' WHERE status = 'running'")
        clock.now += timedelta(minutes=5)
        from tests.test_ingest import detail_html
        return detail_html("1098892")

    http.routes[IDS["1098892"]] = takeover
    report = crawl(settings, http, clock)
    assert report.status == "failed" and any("lock lost" in r for r in report.reasons)
    assert http.count(IDS["1119509"]) == 0


def test_blocked_car_is_backed_off_so_the_next_run_proceeds(settings):
    clock = Clock()
    http = make_http()
    http.routes[IDS["1120464"]] = BlockedError("403 on this lot only")
    assert crawl(settings, http, clock).status == "blocked"
    http2 = make_http()
    report = crawl(settings, http2, clock)
    assert http2.count(IDS["1120464"]) == 0 and report.counts["detailed"] == 2


def test_quarantine_is_swept_at_crawl_start(settings):
    import os
    import time

    from auction_agent.documents import quarantine
    path = quarantine(b"%PDF-1.4 x", "b" * 64, settings)
    old = time.time() - 30 * 86400
    os.utime(path, (old, old))
    crawl(settings, make_http(), Clock())
    assert not path.exists()


@pytest.mark.parametrize("pid", ["1120464"])
def test_late_ct_changes_the_assessment(settings, pid):
    """conformance-12 (c): a CT found later actually changes the score inputs."""
    from auction_agent.scoring.service import rescore_all
    clock = Clock()
    crawl(settings, make_http(with_ct=False), clock)
    with db.open_db(settings.db_path) as conn:
        rescore_all(conn, settings, now=clock.now, use_llm=False)
        before = conn.execute("SELECT a.flags_json FROM assessments a JOIN vehicles v ON v.id=a.vehicle_id "
                              "WHERE v.external_id=?", (pid,)).fetchone()[0]
    clock.now += timedelta(hours=49)
    crawl(settings, make_http(), clock)
    with db.open_db(settings.db_path) as conn:
        rescore_all(conn, settings, now=clock.now, use_llm=False)
        after = conn.execute("SELECT a.flags_json FROM assessments a JOIN vehicles v ON v.id=a.vehicle_id "
                             "WHERE v.external_id=?", (pid,)).fetchone()[0]
    assert "ct_absent" in before and "ct_absent" not in after
