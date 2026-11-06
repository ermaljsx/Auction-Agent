"""The online MVP: snapshot, publish (PC runner), Blob storage and the site's snapshot function."""
import argparse
import importlib.util
import json
import urllib.error
from datetime import datetime, timedelta, timezone

import pytest

from auction_agent import cli, db
from auction_agent.blobstore import SNAPSHOT_PATH, LocalStore, StoreError, VercelBlobStore
from auction_agent.snapshot import build_snapshot, sale_state
from tests.conftest import PROJECT_ROOT, read_fixture
from tests.fakes import FakeHttp
from tests.test_ingest import IDS, PDF, detail_html
from tests.test_service_report import FakeClient

ROOM = "/salle-de-vente-encheres/beauvais"
SALE = "/salle-de-vente-encheres/beauvais/10206"
QUERY = "sort=number_asc&category_view=VP"
# Thursday before the Monday 19 October 2026 sale of the synthetic fixtures.
NOW = datetime(2026, 10, 15, 17, 30, tzinfo=timezone.utc)
ROOM_PAGE = ('<html><body><div class="sale"><h5>Lundi 19 octobre 2026 : Véhicules Utilitaires et Tourisme</h5>'
             f'<a href="{SALE}">Voir la vente</a></div></body></html>')
VIN, PLATE = "VR3UPHNEKNT084478", "FK-482-TR"


def site_routes():
    routes = {ROOM: ROOM_PAGE, f"{SALE}?{QUERY}&page=1": read_fixture("alcopa", "sale_page1.html"),
              f"{SALE}?{QUERY}&page=2": read_fixture("alcopa", "sale_page2.html")}
    for pid, path in IDS.items():
        routes[path] = detail_html(pid, f"CT{pid}")
        routes[f"/getDocument/ct/CT{pid}"] = PDF
    return routes


def publish_args(out, **kw):
    base = dict(house="alcopa", url=ROOM, no_crawl=False, no_llm=False, llm_limit=None,
                out=str(out) if out is not None else None)
    base.update(kw)
    return argparse.Namespace(**base)


class Recording(FakeClient):
    """Stands in for OpenAIClient inside rescore_all (no network)."""

    def __init__(self, *_a, reasoning_effort="none", **_kw):
        super().__init__()
        self.model, self.reasoning_effort, self.notes = "gpt-6-luna", reasoning_effort, []


@pytest.fixture
def site(monkeypatch):
    http = FakeHttp(site_routes())
    monkeypatch.setattr(cli, "PoliteClient", lambda _settings: http)
    monkeypatch.setattr("auction_agent.scoring.service.OpenAIClient", Recording)
    return http


def read_snapshot(out):
    return json.loads((out / SNAPSHOT_PATH).read_text(encoding="utf-8"))


# ------------------------------------------------------------------ publish

def test_publish_crawls_and_never_lists_red_cars(settings, site, tmp_path, capsys):
    """The synthetic lots all carry an unfavourable CT, so all three are red: counted, not listed."""
    out = tmp_path / "out"
    s = settings.replace(openai_api_key="sk-test")
    assert cli.cmd_publish(publish_args(out), s, now=NOW) == 4
    assert "no car could be put forward" in capsys.readouterr().out
    snap = read_snapshot(out)
    assert snap["schema"] == 1 and snap["sale"]["external_id"] == "10206"
    assert snap["sale"]["date"] == "2026-10-19" and snap["sale"]["state"] == "upcoming"
    assert snap["crawl"]["status"] == "ok" and snap["crawl"]["counts"]["detailed"] == 3
    assert snap["counts"]["red"] == 3 and snap["top"] == [] and snap["cards"] == []
    raw = json.dumps(snap, ensure_ascii=False)
    for secret in (VIN, PLATE, "vin_hmac", "registration", "comments", "photo", '"id"'):
        assert secret not in raw


def test_publish_puts_cars_forward_with_ai_verdicts(settings, monkeypatch, tmp_path):
    """Seeded sale of favourable cars, published without a crawl (as after a blocked run)."""
    from tests.test_service_report import NOW as SEED_NOW, seed
    monkeypatch.setattr("auction_agent.scoring.service.OpenAIClient", Recording)
    s = settings.replace(openai_api_key="sk-test")
    with db.open_db(s.db_path) as conn:
        seed(conn)
    out = tmp_path / "out"
    assert cli.cmd_publish(publish_args(out, no_crawl=True), s, now=SEED_NOW) == 0
    snap = read_snapshot(out)
    assert snap["crawl"]["status"] == "skipped" and snap["sale"]["state"] == "upcoming"
    assert snap["top"] and snap["llm"]["calls"] >= 1 and snap["llm"]["models_answered"] == ["gpt-6-luna"]
    assert all(c["verdict"] for c in snap["top"])
    raw = json.dumps(snap, ensure_ascii=False)
    for secret in ("vin_hmac", "registration", "comments", "photo", '"id"'):
        assert secret not in raw
    for c in snap["top"] + snap["cards"]:
        assert c["url"] is None or c["url"].startswith("https://www.alcopa-auction.fr/")
        assert c["tier"] != "red" and len(c["reasons"]) <= 3


def test_provisional_picks_when_no_car_is_complete(settings, monkeypatch, tmp_path):
    from tests.test_service_report import NOW as SEED_NOW, seed
    monkeypatch.setattr("auction_agent.scoring.service.OpenAIClient", Recording)
    s = settings.replace(openai_api_key="sk-test")
    with db.open_db(s.db_path) as conn:
        seed(conn)
        with db.write_tx(conn):
            conn.execute("UPDATE listings SET starting_price = NULL")  # prices not published yet
    out = tmp_path / "out"
    assert cli.cmd_publish(publish_args(out, no_crawl=True), s, now=SEED_NOW) == 0
    snap = read_snapshot(out)
    assert snap["provisional"] is True and snap["top"]
    assert all(c["confidence"] == "partial" and c["verdict"] for c in snap["top"])


def test_publish_without_ai_key_is_flagged(settings, site, tmp_path, capsys):
    out = tmp_path / "out"
    assert cli.cmd_publish(publish_args(out), settings, now=NOW) == 4
    assert "AI review is off" in capsys.readouterr().out
    assert read_snapshot(out)["llm"]["enabled"] is False  # the site still gets the snapshot


def test_blocked_run_keeps_the_last_good_cars(settings, monkeypatch, tmp_path):
    from auction_agent.http_client import BlockedError
    from tests.test_service_report import NOW as SEED_NOW, seed
    monkeypatch.setattr("auction_agent.scoring.service.OpenAIClient", Recording)
    s = settings.replace(openai_api_key="sk-test")
    with db.open_db(s.db_path) as conn:
        seed(conn)
    out = tmp_path / "out"
    assert cli.cmd_publish(publish_args(out, no_crawl=True), s, now=SEED_NOW) == 0
    good = read_snapshot(out)
    monkeypatch.setattr(cli, "PoliteClient", lambda _settings: FakeHttp({ROOM: BlockedError("405 challenge")}))
    assert cli.cmd_publish(publish_args(out), s, now=SEED_NOW + timedelta(hours=2)) == 2
    snap = read_snapshot(out)
    assert snap["crawl"]["status"] == "blocked" and snap["crawl"]["reasons"]
    assert "http" not in " ".join(snap["crawl"]["reasons"])  # URLs are redacted
    assert [c["title"] for c in snap["top"]] == [c["title"] for c in good["top"]]


def test_no_sale_this_week_exits_3_and_still_publishes(settings, monkeypatch, tmp_path):
    http = FakeHttp({ROOM: "<html></html>", "/calendrier-des-ventes": "<html></html>"})
    monkeypatch.setattr(cli, "PoliteClient", lambda _settings: http)
    out = tmp_path / "out"
    assert cli.cmd_publish(publish_args(out), settings, now=NOW) == 3
    snap = read_snapshot(out)
    assert snap["crawl"]["status"] == "no_sale" and snap["sale"] is None and snap["top"] == []


def test_publish_without_blob_token_stops_before_crawling(settings, site, capsys):
    args = publish_args(None)
    assert cli.cmd_publish(args, settings, now=NOW) == 2
    assert "BLOB_READ_WRITE_TOKEN" in capsys.readouterr().out and site.calls == {}


def test_sale_states():
    def row(day):
        start = datetime(2026, 10, 18, 22, 0, tzinfo=timezone.utc)  # Mon 19 Oct 00:00 Paris
        return {"starts_at": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "ends_at": (start + timedelta(hours=23, minutes=59)).strftime("%Y-%m-%dT%H:%M:%SZ")} if day else None
    sale = row(True)
    assert sale_state(sale, datetime(2026, 10, 15, 12, tzinfo=timezone.utc)) == "upcoming"
    assert sale_state(sale, datetime(2026, 10, 19, 5, tzinfo=timezone.utc)) == "today"        # 07:00 Paris
    assert sale_state(sale, datetime(2026, 10, 19, 9, tzinfo=timezone.utc)) == "in_progress"  # 11:00 Paris
    assert sale_state(sale, datetime(2026, 10, 20, 9, tzinfo=timezone.utc)) == "over"
    assert sale_state(None, NOW) == "unknown"


def test_snapshot_lists_no_red_car_and_respects_the_limit(settings, site, tmp_path):
    s = settings.replace(openai_api_key="sk-test")
    cli.cmd_publish(publish_args(tmp_path / "out"), s, now=NOW)
    with db.open_db(s.db_path) as conn:
        sale_id = conn.execute("SELECT id FROM sales").fetchone()[0]
        with db.write_tx(conn):
            conn.execute("UPDATE assessments SET tier = 'red'")
        snap = build_snapshot(conn, s, sale_id=sale_id, now=NOW, public_limit=0)
    assert snap["top"] == [] and snap["cards"] == [] and snap["counts"]["red"] >= 1


# ------------------------------------------------------------------ storage

def test_local_store_writes_atomically_inside_its_folder(tmp_path):
    store = LocalStore(tmp_path / "out")
    store.put(SNAPSHOT_PATH, b"{}", "application/json")
    assert (tmp_path / "out" / SNAPSHOT_PATH).read_bytes() == b"{}"
    with pytest.raises(StoreError):
        store.put("../escape.json", b"{}", "application/json")


def test_vercel_blob_store_checks_the_token(monkeypatch):
    with pytest.raises(StoreError, match="not set"):
        VercelBlobStore()
    with pytest.raises(StoreError, match="malformed"):
        VercelBlobStore("sk-not-a-blob-token")


def test_vercel_blob_store_uploads_privately_and_never_echoes_the_token(monkeypatch):
    calls = []
    monkeypatch.setenv("BLOB_READ_WRITE_TOKEN", "")  # registered, so the store's write is undone

    def fake_put(path, data, **kw):
        calls.append((path, data, kw))

    monkeypatch.setattr("vercel.blob.sync.put_blob", fake_put)
    store = VercelBlobStore("vercel_blob_rw_Abc123_secretpart")
    store.put(SNAPSHOT_PATH, b"{}", "application/json")
    assert calls == [(SNAPSHOT_PATH, b"{}", {"access": "private", "content_type": "application/json",
                                             "allow_overwrite": True, "cache_control_max_age": 60})]

    def boom(*_a, **_kw):
        raise RuntimeError("401 vercel_blob_rw_Abc123_secretpart rejected")

    monkeypatch.setattr("vercel.blob.sync.put_blob", boom)
    with pytest.raises(StoreError) as info:
        store.put(SNAPSHOT_PATH, b"{}", "application/json")
    assert "secretpart" not in str(info.value)


# ------------------------------------------------------------------ the site's snapshot function

def _web_api():
    spec = importlib.util.spec_from_file_location("web_snapshot", PROJECT_ROOT / "web" / "api" / "snapshot.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TOKEN = "vercel_blob_rw_HEl4l39fl2Yk5Yqa_s3cr3tvalue"


def test_blob_url_matches_the_official_sdk():
    from vercel.blob._internal.validation import construct_delivery_url
    api = _web_api()
    assert api.blob_url(TOKEN) == construct_delivery_url("store_HEl4l39fl2Yk5Yqa", "snapshot/latest.json", "private")


class _Response:
    def __init__(self, body):
        self.body = body

    def read(self, _n=-1):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_snapshot_function_answers():
    api = _web_api()
    seen = []

    def ok(request, timeout):
        seen.append((request.full_url, request.headers.get("Authorization"), timeout))
        return _Response(b'{"schema": 1}')

    assert api.load_snapshot(TOKEN, opener=ok) == (200, b'{"schema": 1}')
    assert seen[0][0].endswith("/snapshot/latest.json") and seen[0][1] == f"Bearer {TOKEN}"

    def missing(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)

    def down(request, timeout):
        raise urllib.error.URLError("no route")

    for opener, expected in ((missing, (503, "no_snapshot")), (down, (502, "storage_unavailable")),
                             (lambda r, timeout: _Response(b"<html>"), (502, "snapshot_unreadable"))):
        status, body = api.load_snapshot(TOKEN, opener=opener)
        assert (status, json.loads(body)["status"]) == expected
        assert b"s3cr3t" not in body and b"blob.vercel-storage" not in body
    assert json.loads(api.load_snapshot("", opener=ok)[1])["status"] == "not_configured"
    assert json.loads(api.load_snapshot("sk-wrong", opener=ok)[1])["status"] == "not_configured"


def test_site_files_are_safe():
    app = (PROJECT_ROOT / "web" / "app.js").read_text(encoding="utf-8")
    assert "innerHTML" not in app and "insertAdjacentHTML" not in app and "eval(" not in app
    vercel = json.loads((PROJECT_ROOT / "web" / "vercel.json").read_text(encoding="utf-8"))
    headers = {h["key"]: h["value"] for h in vercel["headers"][0]["headers"]}
    assert "default-src 'self'" in headers["Content-Security-Policy"] and headers["X-Robots-Tag"].startswith("noindex")
    assert (PROJECT_ROOT / "web" / "robots.txt").read_text(encoding="utf-8").strip().endswith("Disallow: /")


def test_windows_task_runs_thursday_evening_and_saturday_and_catches_up():
    task = (PROJECT_ROOT / "scripts" / "windows" / "install_task.ps1").read_text(encoding="utf-8")
    assert "-DaysOfWeek Thursday -At \"19:17\"" in task and "-DaysOfWeek Saturday -At \"09:43\"" in task
    assert "-StartWhenAvailable" in task and "-MultipleInstances IgnoreNew" in task
    runner = (PROJECT_ROOT / "scripts" / "windows" / "run_publish.ps1").read_text(encoding="utf-8")
    assert "auction_agent publish" in runner and "-Encoding utf8" in runner
