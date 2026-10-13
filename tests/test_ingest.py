import json
from datetime import datetime, timedelta, timezone

import pytest

from auction_agent import db
from auction_agent.adapters.alcopa import AlcopaAdapter
from auction_agent.http_client import BlockedError, FetchError
from auction_agent.ingest import CrawlOptions, run_crawl
from tests.conftest import FIXTURES, read_fixture
from tests.fakes import FakeHttp

SALE1 = "/salle-de-vente-encheres/beauvais/10206"
SALE2 = "/salle-de-vente-encheres/beauvais/10300"
PDF = (FIXTURES / "pdf" / "ct_min.pdf").read_bytes()
IDS = {
    "1120464": "/voiture-occasion/peugeot/208-puretech-110-s-s-eat8-allure-1120464",
    "1098892": "/voiture-occasion/renault/clio-iv-1-5-dci-90-energy-business-1098892",
    "1119509": "/voiture-occasion/toyota/yaris-hybride-116h-dynamic-1119509",
}


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 13, 8, 0, tzinfo=timezone.utc)

    def __call__(self):
        self.now += timedelta(seconds=1)
        return self.now


def detail_html(pid: str, ct_token: str = "CTTOKEN1", with_ct: bool = True) -> str:
    html = read_fixture("alcopa", "detail_1120464.html").replace("1120464", pid)
    html = html.replace("d0l6TmpvR3djRzdWbzFZVHB5Z2NzQT09", ct_token)
    if not with_ct:
        html = html.replace(f'<a href="/getDocument/ct/{ct_token}" rel="nofollow" target="_blank">Contrôle technique</a>', "")
    return html


def make_http(sale_path=SALE1, ct_tokens=None, with_ct=True):
    ct_tokens = ct_tokens or {}
    routes = {
        f"{sale_path}?page=1": read_fixture("alcopa", "sale_page1.html"),
        f"{sale_path}?page=2": read_fixture("alcopa", "sale_page2.html"),
    }
    for pid, path in IDS.items():
        token = ct_tokens.get(pid, f"CT{pid}")
        routes[path] = detail_html(pid, token, with_ct)
        routes[f"/getDocument/ct/{token}"] = PDF
    return FakeHttp(routes)


def crawl(settings, http, clock, sale=SALE1, **kw):
    return run_crawl(settings, AlcopaAdapter(http), CrawlOptions(sale_url=f"https://www.alcopa-auction.fr{sale}", **kw), now_fn=clock)


def test_first_crawl_ingests_everything(settings):
    http, clock = make_http(), Clock()
    report = crawl(settings, http, clock)
    assert report.status == "ok"
    assert report.counts["new"] == 3 and report.counts["detailed"] == 3 and report.counts["documents"] == 3
    with db.open_db(settings.db_path) as conn:
        rows = conn.execute("SELECT ingest_state, ct_state, vin_hmac, mileage_km FROM vehicles").fetchall()
        assert all(r["ingest_state"] == "complete" and r["ct_state"] == "present" for r in rows)
        assert all(len(r["vin_hmac"]) == 64 for r in rows)
        docs = conn.execute("SELECT doc_type, fetch_status FROM documents ORDER BY doc_type").fetchall()
        # CT downloaded; maintenance stored as a link only.
        assert sorted({(d["doc_type"], d["fetch_status"]) for d in docs}) == [("ct", "ok"), ("maintenance", "not_fetched")]
        run = conn.execute("SELECT status, scope_json FROM sync_runs").fetchone()
        assert run["status"] == "ok" and json.loads(run["scope_json"])["scoped"] is True


def test_no_clear_text_vin_or_plate_is_stored(settings):
    http, clock = make_http(), Clock()
    crawl(settings, http, clock)
    dump = "\n".join(
        str(v) for table in ("vehicles", "documents", "listings", "sales", "sync_runs")
        for row in _all_rows(settings, table) for v in row
    )
    for secret in ("VR3UPHNEKNT084478", "FK-482-TR", "ZAC5JAC51PJK11650", "GP-755-SL"):
        assert secret not in dump


def _all_rows(settings, table):
    with db.open_db(settings.db_path) as conn:
        return [tuple(r) for r in conn.execute(f"SELECT * FROM {table}")]


def test_recrawl_of_same_sale_fetches_nothing_but_listing(settings):
    http, clock = make_http(), Clock()
    crawl(settings, http, clock)
    before_detail, before_pdf = http.count("/voiture-occasion"), http.count("/getDocument")
    report = crawl(settings, http, clock)
    assert report.counts["new"] == 0 and report.counts["detailed"] == 0
    assert http.count("/voiture-occasion") == before_detail
    assert http.count("/getDocument") == before_pdf


def test_relist_in_new_sale_checks_detail_once_without_pdf(settings):
    clock = Clock()
    crawl(settings, make_http(), clock)
    http2 = make_http(sale_path=SALE2)
    report = crawl(settings, http2, clock, sale=SALE2)
    assert report.counts["relisted"] == 3 and report.counts["new"] == 0
    assert report.counts["detailed"] == 3  # one detail-page check per relisted car
    assert http2.count("/getDocument") == 0  # same CT link: no download
    # A third crawl of the second sale does nothing more.
    report3 = crawl(settings, http2, clock, sale=SALE2)
    assert report3.counts["detailed"] == 0
    with db.open_db(settings.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM listings").fetchone()[0] == 6


def test_relist_with_new_ct_link_downloads_once(settings):
    clock = Clock()
    crawl(settings, make_http(), clock)
    http2 = make_http(sale_path=SALE2, ct_tokens={"1120464": "NEWTOKEN"})
    crawl(settings, http2, clock, sale=SALE2)
    assert http2.count("/getDocument") == 1
    with db.open_db(settings.db_path) as conn:
        url = conn.execute(
            "SELECT d.source_url FROM documents d JOIN vehicles v ON v.id=d.vehicle_id "
            "WHERE v.external_id='1120464' AND d.doc_type='ct'"
        ).fetchone()[0]
        assert url.endswith("/getDocument/ct/NEWTOKEN")


def test_late_ct_is_picked_up_after_48h(settings):
    clock = Clock()
    crawl(settings, make_http(with_ct=False), clock)
    with db.open_db(settings.db_path) as conn:
        assert {r[0] for r in conn.execute("SELECT ct_state FROM vehicles")} == {"absent"}
    http = make_http()
    crawl(settings, http, clock)  # too early: nothing re-checked
    assert http.count("/voiture-occasion") == 0
    clock.now += timedelta(hours=49)
    crawl(settings, http, clock)
    assert http.count("/getDocument") == 3
    with db.open_db(settings.db_path) as conn:
        assert {r[0] for r in conn.execute("SELECT ct_state FROM vehicles")} == {"present"}


def test_limit_makes_run_incomplete_and_scoped(settings):
    report = crawl(settings, make_http(), Clock(), limit=1)
    assert report.status == "incomplete"
    assert any("scoped:limit=1" in r for r in report.reasons)
    assert report.counts["lots_seen"] == 1


def test_listing_failure_fails_the_run_and_releases_the_lock(settings):
    http, clock = make_http(), Clock()
    http.routes[f"{SALE1}?page=2"] = FetchError("timeout")
    with pytest.raises(FetchError):
        crawl(settings, http, clock)
    with db.open_db(settings.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM vehicles").fetchone()[0] == 0
        assert conn.execute("SELECT status FROM sync_runs").fetchone()[0] == "failed"
    assert crawl(settings, make_http(), clock).status == "ok"  # lock was released


def test_block_during_details_stops_cleanly(settings):
    http, clock = make_http(), Clock()
    http.routes[IDS["1098892"]] = BlockedError("405 challenge")
    report = crawl(settings, http, clock)
    assert report.status == "blocked"
    assert http.count(IDS["1119509"]) == 0  # stopped at the block, did not continue
    with db.open_db(settings.db_path) as conn:
        assert conn.execute("SELECT status FROM sync_runs").fetchone()[0] == "blocked"


def test_detail_failure_is_recorded_and_retried_later(settings):
    http, clock = make_http(), Clock()
    http.routes[IDS["1098892"]] = read_fixture("alcopa", "detail_consent.html")
    report = crawl(settings, http, clock)
    assert report.counts["failed"] == 1 and report.status == "ok"
    with db.open_db(settings.db_path) as conn:
        row = conn.execute("SELECT ingest_state, ingest_attempts, last_error FROM vehicles WHERE external_id='1098892'").fetchone()
        assert row["ingest_state"] == "failed" and row["ingest_attempts"] == 1
        assert "specification table" in row["last_error"]
    http.routes[IDS["1098892"]] = detail_html("1098892")
    crawl(settings, http, clock)  # backoff not elapsed yet
    assert http.count(IDS["1098892"]) == 1
    clock.now += timedelta(hours=3)
    crawl(settings, http, clock)
    with db.open_db(settings.db_path) as conn:
        assert conn.execute("SELECT ingest_state FROM vehicles WHERE external_id='1098892'").fetchone()[0] == "complete"


def test_ctrl_c_releases_lock(settings):
    http, clock = make_http(), Clock()

    def boom():
        raise KeyboardInterrupt

    http.routes[IDS["1098892"]] = boom
    with pytest.raises(KeyboardInterrupt):
        crawl(settings, http, clock)
    assert crawl(settings, make_http(), clock).status == "ok"
